"""Spend accounting: a daily dollar cap that concurrent questions cannot overshoot.

Every LLM call goes through :class:`MeteredProvider`, which, before the call,
*reserves* its worst case: the estimated input tokens plus the full
``max_tokens`` of output, at the ceiling prices that OpenRouter is told not to
exceed (``provider.max_price``). The reservation counts against the cap while
the call is in flight, so three questions running at once cannot each see
"$0.01 left" and spend it three times. When the call returns, the reservation
is settled with what OpenRouter says it actually cost (``usage.cost``), or
with tokens x price if it did not say, or with the whole reservation if the
call failed partway — the cap errs towards stopping early, never late.

There are two caps: a global daily one, and a per-user share of it so that one
account cannot spend the whole demo's budget. Days are UTC.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from agent.provider import (
    LLMProvider,
    LLMResponse,
    Message,
    StreamComplete,
    TextDelta,
)

logger = logging.getLogger(__name__)

#: Ceiling prices in USD per million tokens. OpenRouter is told never to route
#: above these, so they bound what any call can cost. Checked 2026-11-22:
#: qwen3-coder is $0.30-0.35 in / $1.00-1.50 out across its providers.
DEFAULT_PRICE_INPUT_PER_M = 0.40
DEFAULT_PRICE_OUTPUT_PER_M = 1.60


def prices_from_env() -> tuple[float, float]:
    return (
        float(os.environ.get("LLM_PRICE_INPUT_PER_M") or DEFAULT_PRICE_INPUT_PER_M),
        float(os.environ.get("LLM_PRICE_OUTPUT_PER_M") or DEFAULT_PRICE_OUTPUT_PER_M),
    )


def utc_day(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(timestamp))


def seconds_until_utc_midnight(timestamp: float) -> int:
    return max(1, int(86_400 - (timestamp % 86_400)))


class BudgetExhausted(Exception):
    """No budget left for this call. ``code`` is ``budget_exhausted`` (the whole
    demo, for today) or ``user_budget_exhausted`` (this user's share)."""

    def __init__(self, code: str, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after


class RequestStopped(Exception):
    """The request was abandoned (``cancelled``) or ran out of time (``timeout``)
    before this call; nothing was spent on it."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Reservation:
    day: str
    github_id: int
    usd: float
    settled: bool = field(default=False, repr=False)


class Budget:
    """The daily caps, over a persistent ledger plus in-memory reservations.

    ``ledger`` needs ``day_spend(day) -> float``, ``user_spend(day, github_id)
    -> float`` and ``add_spend(day, github_id, usd, tokens)``; the app store
    provides them. Reservations live only in this process, which is why the
    server must run as a single worker.
    """

    def __init__(
        self,
        ledger,
        *,
        daily_usd: float,
        per_user_usd: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.ledger = ledger
        self.daily_usd = daily_usd
        self.per_user_usd = per_user_usd
        self._clock = clock
        self._lock = threading.Lock()
        self._in_flight: dict[str, float] = {}
        self._in_flight_by_user: dict[tuple[str, int], float] = {}

    def _exhausted(self, code: str) -> BudgetExhausted:
        retry = seconds_until_utc_midnight(self._clock())
        if code == "budget_exhausted":
            message = "The demo has used its budget for today. It resets at 00:00 UTC."
        else:
            message = "You have used your share of today's demo budget. It resets at 00:00 UTC."
        return BudgetExhausted(code, message, retry)

    def _check(self, day: str, github_id: int, usd: float) -> None:
        # Caller holds self._lock.
        committed = self.ledger.day_spend(day) + self._in_flight.get(day, 0.0)
        if committed + usd > self.daily_usd:
            raise self._exhausted("budget_exhausted")
        mine = self.ledger.user_spend(day, github_id) + self._in_flight_by_user.get(
            (day, github_id), 0.0
        )
        if mine + usd > self.per_user_usd:
            raise self._exhausted("user_budget_exhausted")

    def ensure_available(self, github_id: int, at_least: float) -> None:
        """Raise :class:`BudgetExhausted` unless ``at_least`` could be reserved now."""
        day = utc_day(self._clock())
        with self._lock:
            self._check(day, github_id, at_least)

    def reserve(self, github_id: int, usd: float) -> Reservation:
        day = utc_day(self._clock())
        with self._lock:
            self._check(day, github_id, usd)
            self._in_flight[day] = self._in_flight.get(day, 0.0) + usd
            key = (day, github_id)
            self._in_flight_by_user[key] = self._in_flight_by_user.get(key, 0.0) + usd
        return Reservation(day, github_id, usd)

    def settle(self, reservation: Reservation, actual_usd: float, tokens: int = 0) -> None:
        """Release the reservation and record what was actually spent.

        Charged to the day the call *started*, so a call straddling midnight
        cannot sneak past the cap of either day.
        """
        with self._lock:
            if reservation.settled:
                return
            reservation.settled = True
            day, key = reservation.day, (reservation.day, reservation.github_id)
            self._in_flight[day] = max(0.0, self._in_flight.get(day, 0.0) - reservation.usd)
            self._in_flight_by_user[key] = max(
                0.0, self._in_flight_by_user.get(key, 0.0) - reservation.usd
            )
            self.ledger.add_spend(day, reservation.github_id, max(0.0, actual_usd), tokens)

    def spent_today(self) -> float:
        return self.ledger.day_spend(utc_day(self._clock()))


def _safe_count(text: str) -> int:
    """Token count, falling back to a deliberately high character estimate."""
    try:
        from agent.tokenizer import count_tokens

        return count_tokens(text)
    except Exception:  # tokenizer unavailable: overestimate rather than fail
        logger.warning("tokenizer unavailable; estimating tokens from length", exc_info=True)
        return len(text) // 2 + 1


class MeteredProvider(LLMProvider):
    """Wraps a provider so every call is reserved against, then charged to, a budget.

    Also the place a stopped request stops spending: before each call it
    checks ``cancel`` (set when the client goes away) and ``deadline``, and
    raises :class:`RequestStopped` instead of calling out.
    """

    def __init__(
        self,
        inner: LLMProvider,
        budget: Budget,
        github_id: int,
        *,
        prices: tuple[float, float] | None = None,
        cancel: threading.Event | None = None,
        deadline: float | None = None,
        count: Callable[[str], int] = _safe_count,
    ) -> None:
        self.inner = inner
        self.budget = budget
        self.github_id = github_id
        self.price_in, self.price_out = prices or prices_from_env()
        self.cancel = cancel
        self.deadline = deadline
        self._count = count
        self.spent_usd = 0.0

    # ------------------------------------------------------------ plumbing

    def _guard(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise RequestStopped("cancelled", "The request was cancelled.")
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise RequestStopped("timeout", "That took too long and was stopped.")

    def _usd(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.price_in + output_tokens * self.price_out) / 1_000_000

    def _reserve(self, text: str, max_tokens: int) -> Reservation:
        self._guard()
        return self.budget.reserve(self.github_id, self._usd(self._count(text), max_tokens))

    def _settle(self, reservation: Reservation, response: LLMResponse | None) -> None:
        if response is None:
            # Failed or abandoned partway: assume the worst.
            actual, tokens = reservation.usd, 0
        else:
            tokens = response.input_tokens + response.output_tokens
            if response.cost_usd is not None:
                actual = response.cost_usd
            elif tokens:
                actual = self._usd(response.input_tokens, response.output_tokens)
            else:
                actual = reservation.usd  # no usage reported at all
        self.budget.settle(reservation, actual, tokens)
        self.spent_usd += actual

    def _metered(self, text: str, max_tokens: int, call: Callable[[], LLMResponse]):
        reservation = self._reserve(text, max_tokens)
        response = None
        try:
            response = call()
            return response
        finally:
            self._settle(reservation, response)

    @staticmethod
    def _conversation_text(messages: list[Message], tools: list[dict]) -> str:
        wire = [
            {
                "role": m.role,
                "content": m.content or "",
                "tool_calls": [[c.name, c.arguments] for c in m.tool_calls],
            }
            for m in messages
        ]
        return json.dumps(wire) + json.dumps(tools)

    # ------------------------------------------------------- the interface

    def generate(self, prompt, *, system=None, max_tokens=1024) -> LLMResponse:
        return self._metered(
            (system or "") + prompt,
            max_tokens,
            lambda: self.inner.generate(prompt, system=system, max_tokens=max_tokens),
        )

    def generate_json(self, prompt, json_schema, *, system=None, max_tokens=1024):
        reservation = self._reserve((system or "") + prompt + json.dumps(json_schema), max_tokens)
        response = None
        try:
            payload, response = self.inner.generate_json(
                prompt, json_schema, system=system, max_tokens=max_tokens
            )
            return payload, response
        finally:
            self._settle(reservation, response)

    def converse(self, messages, tools, *, max_tokens=2048) -> LLMResponse:
        return self._metered(
            self._conversation_text(messages, tools),
            max_tokens,
            lambda: self.inner.converse(messages, tools, max_tokens=max_tokens),
        )

    def converse_stream(
        self, messages, tools, *, max_tokens=2048
    ) -> Iterator[TextDelta | StreamComplete]:
        reservation = self._reserve(self._conversation_text(messages, tools), max_tokens)
        complete: LLMResponse | None = None
        try:
            for item in self.inner.converse_stream(messages, tools, max_tokens=max_tokens):
                if isinstance(item, StreamComplete):
                    complete = item.response
                yield item
        finally:
            # Also runs if the consumer stops iterating early.
            self._settle(reservation, complete)
