"""How many questions may be answered at once: a few overall, one per user.

A question holds a slot from before its stream starts until its worker thread
finishes, so a client that disconnects early cannot leave its slot taken, and
a burst of questions from one account cannot crowd everyone else out.
Acquisition never waits: a full server says so (503) and the client retries.
"""

from __future__ import annotations

import threading


class Slot:
    def __init__(self, pool: QuerySlots, github_id: int) -> None:
        self._pool = pool
        self.github_id = github_id
        self._released = False

    def release(self) -> None:
        self._pool._release(self)


class QuerySlots:
    def __init__(self, max_total: int = 3, per_user: int = 1) -> None:
        self.max_total = max_total
        self.per_user = per_user
        self._lock = threading.Lock()
        self._by_user: dict[int, int] = {}
        self._total = 0

    def try_acquire(self, github_id: int) -> tuple[Slot | None, str | None]:
        """A slot, or ``(None, reason)`` with reason ``user_busy`` or ``busy``."""
        with self._lock:
            if self._by_user.get(github_id, 0) >= self.per_user:
                return None, "user_busy"
            if self._total >= self.max_total:
                return None, "busy"
            self._by_user[github_id] = self._by_user.get(github_id, 0) + 1
            self._total += 1
            return Slot(self, github_id), None

    def _release(self, slot: Slot) -> None:
        with self._lock:
            if slot._released:
                return
            slot._released = True
            self._total -= 1
            remaining = self._by_user.get(slot.github_id, 1) - 1
            if remaining:
                self._by_user[slot.github_id] = remaining
            else:
                self._by_user.pop(slot.github_id, None)

    @property
    def in_use(self) -> int:
        with self._lock:
            return self._total
