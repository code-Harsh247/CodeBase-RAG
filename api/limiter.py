"""Rate limiting: an in-memory sliding-window limiter, keyed by user, IP and globally.

Rules are strings like ``"10/hour;25/day"``: every rule must have room, and a
hit is recorded against all of them only when all of them do, so a refused
request does not use up quota. Limits are kept per GitHub user first (accounts
are much harder to mass-produce than IP addresses), with a looser per-IP layer
behind them to catch one person cycling several accounts.

IP addresses are never kept as such: keys use an HMAC of the address under
``APP_SECRET``, and IPv6 addresses are first collapsed to their /64, since one
machine usually holds a whole /64. The client address must be the real one,
so uvicorn runs behind the proxy with ``--proxy-headers
--forwarded-allow-ips=<proxy address>``.

State is in this process only. That, and the in-flight budget reservations,
are why the server runs as a single worker.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass

_UNITS = {"second": 1, "minute": 60, "hour": 3_600, "day": 86_400}
_RULE = re.compile(r"^\s*(\d+)\s*/\s*(\d*)\s*([a-z]+?)s?\s*$")


@dataclass(frozen=True)
class Rule:
    limit: int
    window_s: float


def parse_rules(spec: str) -> tuple[Rule, ...]:
    """``"10/hour;25/day"`` -> rules. Also accepts ``"5/10minutes"``."""
    rules = []
    for part in (spec or "").split(";"):
        if not part.strip():
            continue
        match = _RULE.match(part.lower())
        unit = match and _UNITS.get(match.group(3))
        if not match or unit is None:
            raise ValueError(f"Bad rate limit rule: {part!r} (expected e.g. '10/hour')")
        count = int(match.group(2) or 1)
        rules.append(Rule(limit=int(match.group(1)), window_s=float(count * unit)))
    return tuple(rules)


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic, prune_every: int = 500):
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}
        self._windows: dict[str, float] = {}
        self._prune_every = prune_every
        self._calls = 0

    def hit(self, checks: Iterable[tuple[str, tuple[Rule, ...]]]) -> float | None:
        """Record one hit against every key, unless any rule is full.

        Returns ``None`` when allowed, or the seconds until it would be.
        """
        checks = [(key, rules) for key, rules in checks if rules]
        now = self._clock()
        with self._lock:
            self._calls += 1
            if self._calls % self._prune_every == 0:
                self._prune(now)

            wait = 0.0
            for key, rules in checks:
                hits = self._hits.get(key, ())
                for rule in rules:
                    recent = [t for t in hits if t > now - rule.window_s]
                    if len(recent) >= rule.limit:
                        # The hit that must age out before there is room again.
                        oldest_blocking = recent[len(recent) - rule.limit]
                        wait = max(wait, oldest_blocking + rule.window_s - now)
            if wait > 0:
                return wait

            for key, rules in checks:
                window = max(rule.window_s for rule in rules)
                self._windows[key] = max(self._windows.get(key, 0.0), window)
                hits = self._hits.setdefault(key, deque())
                hits.append(now)
                while hits and hits[0] <= now - self._windows[key]:
                    hits.popleft()
            return None

    def _prune(self, now: float) -> None:
        # Caller holds the lock. Drops keys whose every hit has aged out.
        for key in list(self._hits):
            hits = self._hits[key]
            window = self._windows.get(key, 0.0)
            while hits and hits[0] <= now - window:
                hits.popleft()
            if not hits:
                del self._hits[key]
                self._windows.pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._hits)


def client_ip_key(host: str | None, secret: bytes) -> str:
    """A stable, non-reversible key for the client's address (IPv6 -> its /64)."""
    try:
        address = ipaddress.ip_address(host or "")
        if address.version == 6:
            if address.ipv4_mapped is not None:
                text = str(address.ipv4_mapped)
            else:
                text = str(ipaddress.ip_network(f"{address}/64", strict=False))
        else:
            text = str(address)
    except ValueError:
        text = host or "unknown"
    return hmac.new(secret, text.encode(), hashlib.sha256).hexdigest()[:24]
