"""The daily spend cap: reservations, settlement, and the metered provider."""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest
from conftest import FakeProvider

from agent.metering import (
    Budget,
    BudgetExhausted,
    MeteredProvider,
    RequestStopped,
    seconds_until_utc_midnight,
    utc_day,
)
from agent.openrouter_provider import BASE_URL, OpenRouterProvider
from agent.provider import LLMResponse, Message, ProviderBudgetError, StreamComplete, TextDelta
from api.store import AppStore

NOON = 1_800_000_000 - (1_800_000_000 % 86_400) + 12 * 3600  # some UTC day, 12:00


class Clock:
    def __init__(self, now: float = NOON) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def ledger(tmp_path):
    store = AppStore(tmp_path / "app.db")
    yield store
    store.close()


@pytest.fixture
def budget(ledger, clock):
    return Budget(ledger, daily_usd=0.25, per_user_usd=0.06, clock=clock)


# ------------------------------------------------------------------ budget


def test_reservations_count_before_they_are_spent(budget):
    budget.reserve(1, 0.05)
    with pytest.raises(BudgetExhausted) as caught:
        budget.reserve(1, 0.02)  # 0.05 in flight + 0.02 > 0.06
    assert caught.value.code == "user_budget_exhausted"


def test_settling_records_actual_spend_and_frees_the_rest(budget, ledger, clock):
    reservation = budget.reserve(1, 0.05)
    budget.settle(reservation, 0.001, tokens=500)
    assert ledger.day_spend(utc_day(clock())) == pytest.approx(0.001)
    budget.reserve(1, 0.05)  # the unused 0.049 came back


def test_settling_twice_charges_once(budget, ledger, clock):
    reservation = budget.reserve(1, 0.01)
    budget.settle(reservation, 0.01)
    budget.settle(reservation, 0.01)
    assert ledger.day_spend(utc_day(clock())) == pytest.approx(0.01)


def test_the_global_cap_binds_across_users(budget):
    for user in range(1, 5):
        budget.settle(budget.reserve(user, 0.06), 0.06)  # 0.24 spent
    with pytest.raises(BudgetExhausted) as caught:
        budget.reserve(5, 0.02)
    assert caught.value.code == "budget_exhausted"
    assert 0 < caught.value.retry_after <= 86_400


def test_concurrent_reservations_never_exceed_the_cap(ledger, clock):
    budget = Budget(ledger, daily_usd=0.10, per_user_usd=1.0, clock=clock)
    granted: list[float] = []
    barrier = threading.Barrier(40)

    def ask(user):
        barrier.wait()
        try:
            budget.reserve(user, 0.007)
            granted.append(0.007)
        except BudgetExhausted:
            pass

    threads = [threading.Thread(target=ask, args=(i,)) for i in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(granted) <= 0.10
    assert len(granted) == 14  # floor(0.10 / 0.007)


def test_a_new_utc_day_starts_a_new_budget(budget, clock):
    budget.settle(budget.reserve(1, 0.06), 0.06)
    with pytest.raises(BudgetExhausted):
        budget.ensure_available(1, 0.01)
    clock.now += 12 * 3600 + 1  # past 00:00 UTC
    budget.ensure_available(1, 0.01)


def test_retry_after_points_at_utc_midnight():
    assert seconds_until_utc_midnight(NOON) == 12 * 3600


def test_spend_survives_a_restart(tmp_path, clock):
    store = AppStore(tmp_path / "a.db")
    first = Budget(store, daily_usd=0.25, per_user_usd=0.06, clock=clock)
    first.settle(first.reserve(1, 0.06), 0.06)
    store.close()

    reopened = AppStore(tmp_path / "a.db")
    second = Budget(reopened, daily_usd=0.25, per_user_usd=0.06, clock=clock)
    with pytest.raises(BudgetExhausted):
        second.reserve(1, 0.01)
    reopened.close()


def test_upgrading_an_existing_database_adds_the_ledger(tmp_path):
    import sqlite3

    from api import store as store_module

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(store_module._MIGRATIONS[0] + "\nPRAGMA user_version = 1;")
    db.close()

    upgraded = AppStore(path)
    upgraded.add_spend("2026-01-01", 1, 0.01)
    assert upgraded.day_spend("2026-01-01") == pytest.approx(0.01)
    upgraded.close()


# ------------------------------------------------------- metered provider


def _count(text: str) -> int:
    return len(text) // 4


class CostingProvider(FakeProvider):
    """A FakeProvider whose responses carry a reported cost."""

    def __init__(self, cost: float | None = 0.0004, fail: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.cost = cost
        self.fail = fail
        self.calls = 0

    def converse(self, messages, tools, *, max_tokens=2048):
        self.calls += 1
        if self.fail:
            raise RuntimeError("upstream exploded")
        return LLMResponse(
            text="ok", model="fake", input_tokens=100, output_tokens=20, cost_usd=self.cost
        )


def _metered(budget, inner, **kwargs) -> MeteredProvider:
    return MeteredProvider(inner, budget, 1, prices=(0.40, 1.60), count=_count, **kwargs)


MESSAGES = [Message(role="user", content="what calls send?")]


def test_reported_cost_is_what_gets_charged(budget, ledger, clock):
    provider = _metered(budget, CostingProvider(cost=0.0004))
    provider.converse(MESSAGES, [])
    assert ledger.day_spend(utc_day(clock())) == pytest.approx(0.0004)
    assert provider.spent_usd == pytest.approx(0.0004)


def test_without_a_reported_cost_tokens_times_price_is_charged(budget, ledger, clock):
    _metered(budget, CostingProvider(cost=None)).converse(MESSAGES, [])
    expected = (100 * 0.40 + 20 * 1.60) / 1_000_000
    assert ledger.day_spend(utc_day(clock())) == pytest.approx(expected)


def test_a_failed_call_is_charged_its_worst_case(budget, ledger, clock):
    provider = _metered(budget, CostingProvider(fail=True))
    with pytest.raises(RuntimeError):
        provider.converse(MESSAGES, [], max_tokens=1000)
    assert ledger.day_spend(utc_day(clock())) >= 1000 * 1.60 / 1_000_000


def test_no_call_is_made_once_the_budget_is_gone(ledger, clock):
    budget = Budget(ledger, daily_usd=0.001, per_user_usd=1, clock=clock)
    inner = CostingProvider()
    with pytest.raises(BudgetExhausted):
        _metered(budget, inner).converse(MESSAGES, [], max_tokens=2048)
    assert inner.calls == 0


def test_a_cancelled_request_stops_before_calling(budget):
    cancel = threading.Event()
    cancel.set()
    inner = CostingProvider()
    with pytest.raises(RequestStopped) as caught:
        _metered(budget, inner, cancel=cancel).converse(MESSAGES, [])
    assert caught.value.code == "cancelled"
    assert inner.calls == 0


def test_a_request_past_its_deadline_stops(budget):
    inner = CostingProvider()
    with pytest.raises(RequestStopped) as caught:
        _metered(budget, inner, deadline=time.monotonic() - 1).converse(MESSAGES, [])
    assert caught.value.code == "timeout"
    assert inner.calls == 0


def test_streams_are_charged_from_their_final_usage(budget, ledger, clock):
    events = list(_metered(budget, CostingProvider(cost=0.0007)).converse_stream(MESSAGES, []))
    assert isinstance(events[-1], StreamComplete)
    assert any(isinstance(e, TextDelta) for e in events)
    assert ledger.day_spend(utc_day(clock())) == pytest.approx(0.0007)


def test_an_abandoned_stream_is_charged_its_worst_case(budget, ledger, clock):
    stream = _metered(budget, CostingProvider(cost=0.0001)).converse_stream(
        MESSAGES, [], max_tokens=500
    )
    next(stream)  # first text fragment, then the consumer walks away
    stream.close()
    assert ledger.day_spend(utc_day(clock())) >= 500 * 1.60 / 1_000_000


# ------------------------------------------------------ openrouter wiring


def _openrouter(handler) -> OpenRouterProvider:
    provider = OpenRouterProvider(api_key="test-key")
    provider._client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    return provider


USAGE = {"prompt_tokens": 10, "completion_tokens": 3, "cost": 6e-06}


def test_every_request_carries_the_price_ceiling(monkeypatch):
    monkeypatch.setenv("LLM_PRICE_INPUT_PER_M", "0.4")
    monkeypatch.setenv("LLM_PRICE_OUTPUT_PER_M", "1.6")
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "hi"}}], "usage": USAGE}
        )

    response = _openrouter(handler).generate("hi", max_tokens=5)

    assert seen[0]["provider"] == {"max_price": {"prompt": 0.4, "completion": 1.6}}
    assert response.cost_usd == pytest.approx(6e-06)


def test_streamed_cost_is_read_from_the_final_usage_chunk():
    chunks = [
        {"choices": [{"delta": {"content": "Hi"}}]},
        {"choices": [], "usage": USAGE},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"

    def handler(request):
        assert json.loads(request.content)["provider"]["max_price"]
        return httpx.Response(200, text=body)

    events = list(_openrouter(handler).converse_stream(MESSAGES, []))
    assert events[-1].response.cost_usd == pytest.approx(6e-06)


def test_out_of_credit_is_a_distinct_error():
    provider = _openrouter(lambda request: httpx.Response(402, json={"error": {"code": 402}}))
    with pytest.raises(ProviderBudgetError):
        provider.generate("hi")
