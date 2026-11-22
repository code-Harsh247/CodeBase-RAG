"""The retrieval tools the agent can call.

Each tool returns text meant for the model to read. Failures are returned as
text too, never raised — a tool error is information the agent can act on, not
a reason to abandon the question.

Everything here handles untrusted input twice over: the arguments come from the
model, and the model is reading an arbitrary public repository. So paths are
contained, regexes are time-boxed, lookups run read-only, and every result is
capped before it reaches the prompt.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import regex

from graph.schema import SHARED_LABEL
from retrieval.graph_query import render_rows, run_query
from retrieval.locations import Location, dedupe, locations_from_rows
from retrieval.vector_store import VectorStore

MAX_GREP_MATCHES = 30
MAX_SOURCE_LINES = 120
#: Every tool result is cut to this before the model sees it. Tool output is
#: re-sent on every later hop, so one huge result would be paid for repeatedly.
MAX_TOOL_TEXT = 8_000

#: grep budget. The pattern is treated as hostile: bounded in length, in time,
#: and in how much source it may scan.
MAX_GREP_PATTERN = 200
MAX_GREP_FILES = 3_000
MAX_GREP_BYTES = 20_000_000
MAX_GREP_LINE = 2_000
_REGEX_META = frozenset(".^$*+?{}[]\\|()")
_SKIP_DIRS = frozenset({".git", "__pycache__", ".venv"})

#: Immediate graph context pulled in for every semantic hit. Without this a hit
#: is an isolated snippet; with it the agent sees how the code connects.
_NEIGHBOURHOOD = f"""
MATCH (n:{SHARED_LABEL} {{id: $node_id}})
OPTIONAL MATCH (n)-[:CALLS]->(callee)
OPTIONAL MATCH (caller)-[:CALLS]->(n)
OPTIONAL MATCH (owner:Class)-[:DEFINES]->(n)
RETURN n.qualified_name AS name,
       collect(DISTINCT callee.qualified_name)[..8] AS calls,
       collect(DISTINCT caller.qualified_name)[..8] AS called_by,
       head(collect(DISTINCT owner.qualified_name)) AS defined_in
"""


def _grep_timeout() -> float:
    return float(os.environ.get("GREP_TIMEOUT_SECONDS") or 2)


def cap_text(text: str, limit: int = MAX_TOOL_TEXT) -> str:
    """``text`` cut to ``limit`` characters, saying so when it was cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n… (output truncated)"


@dataclass
class ToolResult:
    ok: bool
    text: str
    #: Source locations this call actually surfaced, taken from structured
    #: fields rather than parsed back out of ``text``. Used to score retrieval
    #: quality in the evaluation harness.
    locations: list[Location] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.text = cap_text(self.text)


class RetrievalTools:
    """Bundles the graph, the semantic index and the checked-out source."""

    def __init__(
        self,
        client,
        repo_id: str,
        repo_path: Path | None = None,
        vector_store: VectorStore | None = None,
    ) -> None:
        self.client = client
        self.repo_id = repo_id
        self.repo_path = Path(repo_path) if repo_path else None
        self.vector_store = vector_store if vector_store is not None else VectorStore()

    # ------------------------------------------------------------ graph_query

    def graph_query(self, cypher: str) -> ToolResult:
        outcome = run_query(self.client, cypher, self.repo_id)
        if not outcome.ok:
            return ToolResult(False, f"Query rejected: {outcome.error}")
        count = f"{outcome.row_count}+" if outcome.truncated else str(outcome.row_count)
        return ToolResult(
            True,
            f"{count} rows.\n{render_rows(outcome.rows, truncated=outcome.truncated)}",
            locations=dedupe(locations_from_rows(outcome.rows)),
        )

    # -------------------------------------------------------- semantic_search

    def semantic_search(self, query: str, limit: int = 6) -> ToolResult:
        hits = self.vector_store.search(self.repo_id, query, limit=limit)
        if not hits:
            return ToolResult(
                True, "No semantic matches. The repository may not be indexed."
            )

        blocks = []
        found: list[Location] = []
        for hit in hits:
            location = f"{hit.file_path}:{hit.start_line}" if hit.start_line else hit.file_path
            block = [f"{hit.qualified_name} ({hit.node_type}, {location})"]
            summary = hit.summary.splitlines()
            if len(summary) > 1:
                block.append("  " + " ".join(summary[1:])[:300])
            block.append(f"  {self._neighbourhood(hit.node_id)}")
            blocks.append("\n".join(block))
            found.append(Location(file=hit.file_path, start_line=hit.start_line))
        return ToolResult(True, "\n".join(blocks), locations=dedupe(found))

    def _neighbourhood(self, node_id: str) -> str:
        try:
            rows = self._read(_NEIGHBOURHOOD, node_id=node_id)
        except Exception:  # noqa: BLE001 - degrade to the hit alone
            return "(graph context unavailable)"
        if not rows:
            return "(no graph context)"

        row = rows[0]
        parts = []
        if row.get("defined_in"):
            parts.append(f"method of {row['defined_in']}")
        calls = [name for name in (row.get("calls") or []) if name]
        called_by = [name for name in (row.get("called_by") or []) if name]
        if calls:
            parts.append(f"calls: {', '.join(calls)}")
        if called_by:
            parts.append(f"called by: {', '.join(called_by)}")
        return " | ".join(parts) if parts else "(no callers or callees recorded)"

    # --------------------------------------------------------------- read_code

    def read_code(self, qualified_name: str) -> ToolResult:
        """Source of a definition, or of a whole file when given a path.

        The model reaches for a file path here as readily as a symbol name, so
        both are accepted rather than failing the hop on a reasonable guess.
        """
        if self.repo_path is not None and (
            qualified_name.endswith(".py") or "/" in qualified_name
        ):
            found = self._read_file(qualified_name)
            if found is not None:
                return found

        rows = self._read(
            f"MATCH (n:{SHARED_LABEL} {{repo_id: $repo_id}}) "
            f"WHERE n.qualified_name = $qualified_name OR n.name = $qualified_name "
            f"RETURN n.file_path AS file, n.start_line AS start, n.end_line AS end, "
            f"n.qualified_name AS name LIMIT 3",
            repo_id=self.repo_id,
            qualified_name=qualified_name,
        )
        if not rows:
            return ToolResult(False, f"No definition named {qualified_name!r} in the graph.")
        if self.repo_path is None:
            return ToolResult(False, "Source is not available locally.")

        blocks = []
        found: list[Location] = []
        for row in rows:
            # file_path came from ingesting an untrusted repository, so it gets
            # the same containment check as a path the model typed.
            path = self._safe_path(row.get("file") or "")
            if path is None or not path.is_file() or not row.get("start"):
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(int(row["start"]) - 1, 0)
            end = min(int(row.get("end") or row["start"]), start + MAX_SOURCE_LINES)
            body = "\n".join(
                f"{number:>5}  {text}"
                for number, text in enumerate(lines[start:end], start=start + 1)
            )
            blocks.append(f"{row['name']} — {row['file']}:{row['start']}\n{body}")
            found.append(
                Location(file=row["file"], start_line=int(row["start"]), end_line=end)
            )

        if not blocks:
            return ToolResult(False, f"Could not read source for {qualified_name!r}.")
        return ToolResult(True, "\n\n".join(blocks), locations=dedupe(found))

    def _read_file(self, path_like: str) -> ToolResult | None:
        """Read a file by repo-relative path; None when it is not one."""
        candidate = self._safe_path(path_like)
        if candidate is None:
            return ToolResult(False, "Path is outside the repository.")
        if not candidate.is_file():
            return None

        lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        shown = lines[:MAX_SOURCE_LINES]
        body = "\n".join(
            f"{number:>5}  {text}" for number, text in enumerate(shown, start=1)
        )
        if len(lines) > MAX_SOURCE_LINES:
            body += f"\n… {len(lines) - MAX_SOURCE_LINES} more lines; ask for a symbol instead"
        shown_range = Location(
            file=path_like.replace("\\", "/"), start_line=1, end_line=len(shown)
        )
        return ToolResult(True, f"{path_like}\n{body}", locations=[shown_range])

    # ----------------------------------------------------------------- helpers

    def _read(self, query: str, **params) -> list[dict]:
        """Fixed lookups go through the read-only, bounded path when available."""
        run_read = getattr(self.client, "run_read", None)
        if run_read is None:  # test doubles
            return self.client.run(query, **params)
        return run_read(query, params, timeout_s=5, max_rows=50)

    def _safe_path(self, relative: str) -> Path | None:
        """``relative`` resolved inside the repository, or None if it escapes.

        ``resolve()`` follows symlinks, so a link inside the repo pointing at a
        file elsewhere on the server resolves outside the root and is refused.
        """
        if self.repo_path is None or not relative:
            return None
        root = self.repo_path.resolve()
        candidate = (root / relative).resolve()
        if candidate != root and root not in candidate.parents:
            return None
        return candidate

    # -------------------------------------------------------------------- grep

    def grep(self, pattern: str) -> ToolResult:
        """Regex search over the checked-out source, within a fixed budget.

        Uses the ``regex`` engine rather than ``re`` because it supports a match
        timeout and releases the GIL while matching: a catastrophically
        backtracking pattern returns an error the model can act on instead of
        freezing the server. Plain words skip the regex engine entirely.
        """
        if self.repo_path is None:
            return ToolResult(False, "Source is not available locally.")
        if not pattern:
            return ToolResult(False, "Pattern is empty.")
        if len(pattern) > MAX_GREP_PATTERN:
            return ToolResult(
                False, f"Pattern is too long (max {MAX_GREP_PATTERN} characters)."
            )

        literal = not (set(pattern) & _REGEX_META)
        compiled = None
        if not literal:
            try:
                compiled = regex.compile(pattern)
            except regex.error as exc:
                return ToolResult(False, f"Invalid regular expression: {exc}")

        deadline = time.monotonic() + _grep_timeout()
        root = self.repo_path.resolve()
        matches: list[str] = []
        found: list[Location] = []
        files = 0
        scanned = 0

        for path in sorted(self.repo_path.rglob("*.py")):
            if path.is_symlink() or any(part in _SKIP_DIRS for part in path.parts):
                continue
            if root not in path.resolve().parents:  # under a symlinked directory
                continue
            files += 1
            if files > MAX_GREP_FILES or scanned > MAX_GREP_BYTES:
                break
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += len(text)
            relative = path.relative_to(self.repo_path).as_posix()

            for number, line in enumerate(text.splitlines(), start=1):
                line = line[:MAX_GREP_LINE]
                if literal:
                    hit = pattern in line
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return _too_expensive()
                    try:
                        hit = compiled.search(line, timeout=remaining, concurrent=True)
                    except TimeoutError:
                        return _too_expensive()
                if hit:
                    matches.append(f"{relative}:{number}: {line.strip()[:160]}")
                    found.append(Location(file=relative, start_line=number))
                    if len(matches) >= MAX_GREP_MATCHES:
                        return ToolResult(
                            True,
                            "\n".join(matches) + f"\n(stopped at {MAX_GREP_MATCHES} matches)",
                            locations=dedupe(found),
                        )
        if not matches:
            return ToolResult(True, f"No matches for {pattern!r}.")
        return ToolResult(True, "\n".join(matches), locations=dedupe(found))


def _too_expensive() -> ToolResult:
    return ToolResult(
        False, "Pattern is too expensive to run. Use a simpler pattern or a plain word."
    )
