"""Rate limits, query slots, and workers that outlive their streams."""

from __future__ import annotations

import dataclasses
import json
import threading

import pytest
from conftest import FakeProvider

from api import server
from api.concurrency import QuerySlots
from api.limiter import RateLimiter, Rule, client_ip_key, parse_rules
from api.settings import Settings


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ------------------------------------------------------------------ rules


def test_rule_strings():
    assert parse_rules("10/hour;25/day") == (Rule(10, 3600), Rule(25, 86_400))
    assert parse_rules("5/10minutes") == (Rule(5, 600),)
    assert parse_rules("3 / seconds") == (Rule(3, 1),)
    assert parse_rules("") == ()


@pytest.mark.parametrize("spec", ["ten/hour", "10/fortnight", "10", "10/"])
def test_bad_rule_strings_are_refused(spec):
    with pytest.raises(ValueError):
        parse_rules(spec)


def test_bad_rules_stop_startup():
    with pytest.raises(ValueError):
        Settings(rate_limit_query="lots/hour").check()


# ---------------------------------------------------------------- limiter


def test_sliding_window_allows_then_refuses_then_recovers():
    clock = Clock()
    limiter = RateLimiter(clock=clock)
    rules = parse_rules("2/minute")
    assert limiter.hit([("k", rules)]) is None
    clock.now += 10
    assert limiter.hit([("k", rules)]) is None
    clock.now += 10
    assert limiter.hit([("k", rules)]) == pytest.approx(40)  # first hit ages out at +60
    clock.now += 40.5
    assert limiter.hit([("k", rules)]) is None


def test_the_longest_rule_binds():
    clock = Clock()
    limiter = RateLimiter(clock=clock)
    rules = parse_rules("2/minute;3/hour")
    for _ in range(3):
        assert limiter.hit([("k", rules)]) is None
        clock.now += 61
    wait = limiter.hit([("k", rules)])
    assert wait is not None and wait > 3000


def test_a_refused_request_uses_no_quota():
    limiter = RateLimiter(clock=Clock())
    user, ip = parse_rules("5/hour"), parse_rules("1/hour")
    assert limiter.hit([("user", user), ("ip", ip)]) is None
    for _ in range(10):
        assert limiter.hit([("user", user), ("ip", ip)]) is not None
    # The user key recorded only the one allowed hit.
    for _ in range(4):
        assert limiter.hit([("user", user)]) is None


def test_stale_keys_are_pruned():
    clock = Clock()
    limiter = RateLimiter(clock=clock, prune_every=10)
    for i in range(9):
        limiter.hit([(f"k{i}", parse_rules("1/minute"))])
    clock.now += 120
    limiter.hit([("fresh", parse_rules("1/minute"))])
    assert len(limiter) == 1


def test_ip_keys_hide_the_address_and_group_ipv6_by_64():
    secret = b"s" * 32
    same_64 = client_ip_key("2001:db8:1:2::1", secret), client_ip_key("2001:db8:1:2::ffff", secret)
    assert same_64[0] == same_64[1]
    assert client_ip_key("2001:db8:1:3::1", secret) != same_64[0]
    assert client_ip_key("::ffff:203.0.113.9", secret) == client_ip_key("203.0.113.9", secret)
    assert "203.0.113.9" not in client_ip_key("203.0.113.9", secret)
    assert client_ip_key("203.0.113.9", b"t" * 32) != client_ip_key("203.0.113.9", secret)


def test_production_needs_an_app_secret():
    production = Settings(
        app_env="production",
        github_client_id="x",
        github_client_secret="y",
        public_base_url="https://demo.example",
    )
    with pytest.raises(RuntimeError, match="APP_SECRET"):
        production.check()
    dataclasses.replace(production, app_secret="k" * 32).check()


# ------------------------------------------------------------ query slots


def test_one_question_per_user_and_a_few_overall():
    slots = QuerySlots(max_total=2, per_user=1)
    first, _ = slots.try_acquire(1)
    assert slots.try_acquire(1) == (None, "user_busy")
    second, _ = slots.try_acquire(2)
    assert slots.try_acquire(3) == (None, "busy")
    first.release()
    first.release()  # idempotent
    assert slots.in_use == 1
    assert slots.try_acquire(3)[0] is not None
    second.release()


# --------------------------------------------------------------------- API


def _events(response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


class _NoTools:
    def __init__(self, *args, **kwargs):
        pass


@pytest.fixture
def answering(web, monkeypatch):
    """The web app, able to answer questions with a fake model."""
    monkeypatch.setattr(server, "get_provider", FakeProvider)
    monkeypatch.setattr(server, "RetrievalTools", _NoTools)
    return web


def _limits(web, **rules) -> None:
    web.app.state.settings = dataclasses.replace(web.app.state.settings, **rules)


def _ready(web, repo_id, *users):
    web.store.mark_ready(repo_id)
    for user in users:
        web.store.add_owner(user.github_id, repo_id)


def _ask(web, repo_id="a/lib"):
    return web.client.post("/api/query", json={"repo_id": repo_id, "question": "hi"})


def test_questions_are_rate_limited_per_user(answering):
    web = answering
    alice = web.login_as("alice")
    _ready(web, "a/lib", alice)
    _limits(web, rate_limit_query="2/hour")

    assert _ask(web).status_code == 200
    assert _ask(web).status_code == 200
    refused = _ask(web)

    assert refused.status_code == 429
    assert refused.json()["detail"]["code"] == "rate_limited"
    assert 0 < int(refused.headers["retry-after"]) <= 3600


def test_the_ip_layer_catches_account_cycling(answering):
    web = answering
    _limits(web, rate_limit_query="10/hour", rate_limit_query_ip="2/hour")
    for login in ("sock1", "sock2", "sock3"):
        user = web.login_as(login)
        _ready(web, "a/lib", user)
    # All three accounts come from the same test client address.
    statuses = []
    for login in ("sock1", "sock2", "sock3"):
        web.login_as(login)
        statuses.append(_ask(web).status_code)
    assert statuses == [200, 200, 429]


def test_admins_are_not_rate_limited(answering):
    web = answering
    admin = web.login_as("admin")
    _ready(web, "a/lib", admin)
    _limits(web, rate_limit_query="1/hour", rate_limit_query_ip="1/hour")
    assert [_ask(web).status_code for _ in range(3)] == [200, 200, 200]


def test_slots_are_released_when_answers_finish(answering):
    web = answering
    alice = web.login_as("alice")
    _ready(web, "a/lib", alice)
    for _ in range(3):
        assert _events(_ask(web))[-1]["type"] == "answer"
    assert web.app.state.query_slots.in_use == 0


def test_a_second_concurrent_question_from_one_user_is_refused(answering):
    web = answering
    alice = web.login_as("alice")
    _ready(web, "a/lib", alice)
    held, _ = web.app.state.query_slots.try_acquire(alice.github_id)

    response = _ask(web)

    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "user_busy"
    held.release()


def test_a_full_server_is_a_503(answering):
    web = answering
    alice = web.login_as("alice")
    _ready(web, "a/lib", alice)
    slots = web.app.state.query_slots
    held = [slots.try_acquire(1000 + i)[0] for i in range(slots.max_total)]

    response = _ask(web)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "busy"
    assert response.headers["retry-after"]
    for slot in held:
        slot.release()


def test_new_ingests_have_a_global_daily_cap(web, monkeypatch):
    from ingestion.pipeline import IngestionSummary

    monkeypatch.setattr(
        server,
        "ingest",
        lambda source, client, **k: IngestionSummary(
            repo_id=source.removeprefix("https://github.com/"), commit="c",
            node_counts={"Function": 1}, edge_counts={},
        ),
    )
    _limits(web, rate_limit_ingest_global="2/day", rate_limit_ingest_ip="100/day")
    statuses = []
    for i, login in enumerate(("u1", "u2", "u3")):
        web.login_as(login)
        response = web.client.post("/api/ingest", json={"url": f"https://github.com/o/r{i}"})
        statuses.append(response.status_code)
    assert statuses == [200, 200, 429]


def test_adding_an_indexed_repo_has_its_own_looser_limit(web, monkeypatch):
    monkeypatch.setattr(server, "ingest", lambda *a, **k: pytest.fail("must not clone"))
    _limits(web, rate_limit_ingest="1/hour", rate_limit_add="3/hour")
    alice = web.login_as("alice")
    for i in range(3):
        web.store.mark_ready(f"o/r{i}")
    statuses = [
        web.client.post("/api/ingest", json={"url": f"https://github.com/o/r{i}"}).status_code
        for i in range(3)
    ]
    assert statuses == [200, 200, 200]
    assert web.store.owned_count(alice.github_id) == 3


def test_sign_in_is_rate_limited_per_ip(web):
    _limits(web, rate_limit_login_ip="2/hour")
    locations = [
        web.client.get("/api/auth/login", follow_redirects=False).headers["location"]
        for _ in range(3)
    ]
    assert locations[0].startswith("https://github.com/")
    assert locations[2] == "/?auth_error=rate_limited"


def test_summaries_are_rate_limited(web, monkeypatch):
    monkeypatch.setattr(server, "get_provider", FakeProvider)
    web.login_as("alice")
    _limits(web, rate_limit_summarize="1/hour")
    body = {"turns": [{"role": "user", "content": "hi"}]}
    assert web.client.post("/api/summarize", json=body).status_code == 200
    assert web.client.post("/api/summarize", json=body).status_code == 429


# ------------------------------------------------- workers outlive streams


def test_a_question_finishes_even_if_its_stream_is_never_read(monkeypatch):
    def broken_client():
        raise RuntimeError("no graph")

    monkeypatch.setattr(server, "_client", broken_client)
    finished = threading.Event()
    request = server.QueryRequest(repo_id="a/b", question="hi")

    server._stream_answer(
        request, "a/b", FakeProvider(), threading.Event(), deadline=1e12,
        on_finish=finished.set,
    )  # the returned stream is dropped unread, as on an instant disconnect

    assert finished.wait(5), "the slot would never be released"


def test_an_ingest_settles_even_if_its_stream_is_never_read(web, monkeypatch):
    from ingestion.limits import IngestLimitError

    def refuse(*args, **kwargs):
        raise IngestLimitError("repo_too_large", "Too big.")

    monkeypatch.setattr(server, "ingest", refuse)
    user = web.login_as("alice")
    coordinator = web.app.state.coordinator
    ticket = coordinator.try_enqueue()
    web.store.begin_indexing("a/b")
    current = server.CurrentUser(user.github_id, user.login, "", False)

    server._stream_ingest(web.app, current, "a/b", refresh=False, fresh=True, ticket=ticket)

    for _ in range(100):
        if coordinator.outstanding == 0 and web.store.get_repo("a/b") is None:
            break
        threading.Event().wait(0.05)
    assert coordinator.outstanding == 0
    assert web.store.get_repo("a/b") is None
