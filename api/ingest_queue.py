"""One ingestion at a time, a short line behind it, and per-repository locks.

Ingestion is the heaviest thing this server does (clone, parse, embed on a
4-core VM), so it is serialised: one runs, up to ``max_waiting`` wait their
turn in order, and anyone past that is told the server is busy *before* their
stream starts. Waiting tickets report their place in line.

The per-repository locks are shared by everything that changes a repository's
data — ingestion, removal and eviction — so, for example, a user adding a repo
at the same moment its last owner removes it cannot end with the data deleted
under the new owner.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager

from ingestion.limits import IngestLimitError


class Ticket:
    """A place in line. ``wait()`` until it is this ticket's turn, then ``release()``."""

    def __init__(self, coordinator: IngestCoordinator) -> None:
        self._coordinator = coordinator
        self._active = False
        self._done = False

    def wait(
        self,
        cancel: threading.Event | None = None,
        on_position: Callable[[int], None] | None = None,
        poll_s: float = 1.0,
    ) -> None:
        """Block until this ticket may run.

        ``on_position(n)`` is told how many are ahead whenever that changes.
        Raises ``IngestLimitError("cancelled")`` if ``cancel`` is set first, in
        which case the ticket is already released.
        """
        self._coordinator._wait(self, cancel, on_position, poll_s)

    def release(self) -> None:
        self._coordinator._release(self)


class IngestCoordinator:
    def __init__(self, workers: int = 1, max_waiting: int = 3) -> None:
        self.workers = workers
        self.max_waiting = max_waiting
        self._cond = threading.Condition()
        self._line: deque[Ticket] = deque()
        self._running = 0
        self._repo_locks: dict[str, threading.Lock] = {}
        self._repo_locks_guard = threading.Lock()

    # ------------------------------------------------------------- the line

    def try_enqueue(self) -> Ticket | None:
        """A ticket, or None when the line is already full."""
        with self._cond:
            if self._running + len(self._line) >= self.workers + self.max_waiting:
                return None
            ticket = Ticket(self)
            self._line.append(ticket)
            return ticket

    def _wait(self, ticket, cancel, on_position, poll_s) -> None:
        last_position = None
        with self._cond:
            while True:
                if cancel is not None and cancel.is_set():
                    self._forget(ticket)
                    raise IngestLimitError("cancelled", "Indexing was cancelled.")
                position = self._line.index(ticket)
                if position == 0 and self._running < self.workers:
                    self._line.popleft()
                    self._running += 1
                    ticket._active = True
                    return
                ahead = position + self._running
                if on_position is not None and ahead != last_position:
                    last_position = ahead
                    on_position(ahead)
                self._cond.wait(poll_s)

    def _release(self, ticket: Ticket) -> None:
        with self._cond:
            self._forget(ticket)

    def _forget(self, ticket: Ticket) -> None:
        # Caller holds self._cond.
        if ticket._done:
            return
        ticket._done = True
        if ticket._active:
            self._running -= 1
        elif ticket in self._line:
            self._line.remove(ticket)
        self._cond.notify_all()

    @property
    def outstanding(self) -> int:
        with self._cond:
            return self._running + len(self._line)

    # -------------------------------------------------------- repo locks

    def _lock_for(self, repo_id: str) -> threading.Lock:
        key = repo_id.lower()
        with self._repo_locks_guard:
            lock = self._repo_locks.get(key)
            if lock is None:
                lock = self._repo_locks[key] = threading.Lock()
            return lock

    @contextmanager
    def repo_lock(self, repo_id: str, timeout_s: float = -1):
        """Hold the lock for ``repo_id``. Raises TimeoutError if not acquired in time."""
        lock = self._lock_for(repo_id)
        if not lock.acquire(timeout=timeout_s):
            raise TimeoutError(repo_id)
        try:
            yield
        finally:
            lock.release()
