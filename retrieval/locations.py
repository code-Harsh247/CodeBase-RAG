"""Source locations, as retrieved by a system or asserted by ground truth.

Retrieval quality is measured by asking whether the code a question is really
about was put in front of the model. That requires both systems under
comparison to report locations in the same shape, extracted from structured
fields rather than scraped out of rendered text — otherwise the metric measures
the scraper.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: How far a point location may be from the expected line and still count.
#: Definition start lines are exact in the graph, but a naive chunk boundary or
#: a decorator line can shift things slightly; a few lines of slack keeps the
#: metric about "found the right code" rather than off-by-one bookkeeping.
LINE_TOLERANCE = 3

_LOCATION = re.compile(r"^(?P<file>[^:]+?)(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?$")


@dataclass(frozen=True)
class Location:
    """A file, optionally narrowed to a line or a line range."""

    file: str
    start_line: int | None = None
    end_line: int | None = None

    @classmethod
    def parse(cls, text: str) -> Location:
        """``src/x.py``, ``src/x.py:42``, or ``src/x.py:42-80``."""
        match = _LOCATION.match(text.strip())
        if match is None:
            raise ValueError(f"Unparseable location: {text!r}")
        start = match.group("start")
        end = match.group("end")
        return cls(
            file=match.group("file").replace("\\", "/"),
            start_line=int(start) if start else None,
            end_line=int(end) if end else None,
        )

    def covers(self, expected: Location) -> bool:
        """True when this retrieved location accounts for ``expected``."""
        if self.file != expected.file:
            return False
        # A whole-file expectation is satisfied by any hit in that file, and a
        # whole-file retrieval satisfies any expectation within it.
        if expected.start_line is None or self.start_line is None:
            return True

        low = self.start_line - LINE_TOLERANCE
        high = (self.end_line or self.start_line) + LINE_TOLERANCE
        return low <= expected.start_line <= high

    def __str__(self) -> str:
        if self.start_line is None:
            return self.file
        if self.end_line and self.end_line != self.start_line:
            return f"{self.file}:{self.start_line}-{self.end_line}"
        return f"{self.file}:{self.start_line}"


def locations_from_rows(rows: list[dict]) -> list[Location]:
    """Pull locations out of Cypher result rows.

    Queries are prompted to return `file_path` and `start_line`, but the model
    names its columns freely, so this accepts the common aliases rather than
    silently scoring a good retrieval as a miss.
    """
    file_keys = ("file", "file_path", "path", "filepath")
    line_keys = ("line", "start_line", "startline", "start")
    end_keys = ("end_line", "endline", "end")

    found: list[Location] = []
    for row in rows:
        lowered = {str(key).lower(): value for key, value in row.items()}
        file_value = next((lowered[k] for k in file_keys if lowered.get(k)), None)
        if not isinstance(file_value, str):
            continue
        line_value = next((lowered[k] for k in line_keys if lowered.get(k)), None)
        end_value = next((lowered[k] for k in end_keys if lowered.get(k)), None)
        found.append(
            Location(
                file=file_value.replace("\\", "/"),
                start_line=_as_int(line_value),
                end_line=_as_int(end_value),
            )
        )
    return found


def _as_int(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number or None


def dedupe(locations: list[Location]) -> list[Location]:
    """Stable de-duplication, preserving first-seen order."""
    seen: set[Location] = set()
    unique: list[Location] = []
    for location in locations:
        if location not in seen:
            seen.add(location)
            unique.append(location)
    return unique
