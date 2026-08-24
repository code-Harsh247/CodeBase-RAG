"""The retrieval tools the agent can call.

Each tool returns text meant for the model to read. Failures are returned as
text too, never raised — a tool error is information the agent can act on, not
a reason to abandon the question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from graph.schema import SHARED_LABEL
from retrieval.graph_query import render_rows, run_query
from retrieval.vector_store import VectorStore

MAX_GREP_MATCHES = 30
MAX_SOURCE_LINES = 120

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


@dataclass
class ToolResult:
    ok: bool
    text: str


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
        return ToolResult(
            True, f"{outcome.row_count} rows.\n{render_rows(outcome.rows)}"
        )

    # -------------------------------------------------------- semantic_search

    def semantic_search(self, query: str, limit: int = 6) -> ToolResult:
        hits = self.vector_store.search(self.repo_id, query, limit=limit)
        if not hits:
            return ToolResult(
                True, "No semantic matches. The repository may not be indexed."
            )

        blocks = []
        for hit in hits:
            location = f"{hit.file_path}:{hit.start_line}" if hit.start_line else hit.file_path
            block = [f"{hit.qualified_name} ({hit.node_type}, {location})"]
            summary = hit.summary.splitlines()
            if len(summary) > 1:
                block.append("  " + " ".join(summary[1:])[:300])
            block.append(f"  {self._neighbourhood(hit.node_id)}")
            blocks.append("\n".join(block))
        return ToolResult(True, "\n".join(blocks))

    def _neighbourhood(self, node_id: str) -> str:
        try:
            rows = self.client.run(_NEIGHBOURHOOD, node_id=node_id)
        except Exception as exc:  # noqa: BLE001 - degrade to the hit alone
            return f"(graph context unavailable: {exc})"
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

        rows = self.client.run(
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
        for row in rows:
            path = self.repo_path / row["file"]
            if not path.exists() or not row.get("start"):
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(int(row["start"]) - 1, 0)
            end = min(int(row.get("end") or row["start"]), start + MAX_SOURCE_LINES)
            body = "\n".join(
                f"{number:>5}  {text}"
                for number, text in enumerate(lines[start:end], start=start + 1)
            )
            blocks.append(f"{row['name']} — {row['file']}:{row['start']}\n{body}")

        if not blocks:
            return ToolResult(False, f"Could not read source for {qualified_name!r}.")
        return ToolResult(True, "\n\n".join(blocks))

    def _read_file(self, path_like: str) -> ToolResult | None:
        """Read a file by repo-relative path; None when it is not one."""
        assert self.repo_path is not None
        candidate = (self.repo_path / path_like).resolve()
        root = self.repo_path.resolve()
        # A path from the model is untrusted input: keep reads inside the repo.
        if root not in candidate.parents and candidate != root:
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
        return ToolResult(True, f"{path_like}\n{body}")

    # -------------------------------------------------------------------- grep

    def grep(self, pattern: str) -> ToolResult:
        """Literal-ish text search over the checked-out source."""
        if self.repo_path is None:
            return ToolResult(False, "Source is not available locally.")
        try:
            re.compile(pattern)
        except re.error as exc:
            return ToolResult(False, f"Invalid regular expression: {exc}")

        matches: list[str] = []
        for path in sorted(self.repo_path.rglob("*.py")):
            if any(part in {".git", "__pycache__", ".venv"} for part in path.parts):
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            relative = path.relative_to(self.repo_path).as_posix()
            for number, line in enumerate(lines, start=1):
                if re.search(pattern, line):
                    matches.append(f"{relative}:{number}: {line.strip()[:160]}")
                    if len(matches) >= MAX_GREP_MATCHES:
                        return ToolResult(
                            True,
                            "\n".join(matches) + f"\n(stopped at {MAX_GREP_MATCHES} matches)",
                        )
        if not matches:
            return ToolResult(True, f"No matches for {pattern!r}.")
        return ToolResult(True, "\n".join(matches))
