"""What this server is willing to ingest, and the error raised past it."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


class IngestLimitError(Exception):
    """A repository broke one of the ingestion limits.

    ``code`` is stable and machine-readable (the UI maps it to a message);
    ``message`` is safe to show a user — it never carries paths or internals.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class IngestLimits:
    """Bounds on one ingestion. ``None`` means unbounded (the CLI's default).

    The web server builds one per request, so a hostile or merely huge
    repository fails fast with a clear reason instead of tying up the machine.
    """

    max_files: int | None = None
    max_total_bytes: int | None = None
    max_graph_nodes: int | None = None
    clone_timeout_s: float | None = None
    max_clone_disk_bytes: int | None = None
    #: ``time.monotonic()`` value after which the ingestion is abandoned.
    deadline: float | None = None
    cancel: threading.Event | None = None

    @classmethod
    def unbounded(cls) -> IngestLimits:
        return cls()

    def check(self) -> None:
        """Raise if the ingestion should stop now. Cheap; call it often."""
        if self.cancel is not None and self.cancel.is_set():
            raise IngestLimitError("cancelled", "Indexing was cancelled.")
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise IngestLimitError("timeout", "Indexing took too long and was stopped.")
