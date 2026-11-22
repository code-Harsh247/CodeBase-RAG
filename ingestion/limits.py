"""The error raised when a repository exceeds what this server will ingest."""

from __future__ import annotations


class IngestLimitError(Exception):
    """A repository broke one of the ingestion limits.

    ``code`` is stable and machine-readable (the UI maps it to a message);
    ``message`` is safe to show a user — it never carries paths or internals.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
