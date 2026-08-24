"""Execute validated Cypher and render results for the model to read."""

from __future__ import annotations

from dataclasses import dataclass, field

from retrieval.cypher_guard import Validation, explain, validate_cypher

#: Beyond this, rows are truncated — a wall of results crowds out the question.
MAX_ROWS_RENDERED = 40
MAX_VALUE_CHARS = 200


@dataclass
class QueryOutcome:
    ok: bool
    cypher: str
    rows: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def row_count(self) -> int:
        return len(self.rows)


def run_query(client, cypher: str, repo_id: str) -> QueryOutcome:
    """Validate, plan, then execute. Errors come back as text the model can fix."""
    params = {"repo_id": repo_id}

    static: Validation = validate_cypher(cypher)
    if not static:
        return QueryOutcome(False, cypher, error=static.error)

    planned: Validation = explain(client, cypher, params)
    if not planned:
        return QueryOutcome(False, cypher, error=planned.error)

    try:
        rows = client.run(cypher, **params)
    except Exception as exc:  # noqa: BLE001 - surfaced to the model for a retry
        return QueryOutcome(False, cypher, error=f"Execution failed: {exc}")

    return QueryOutcome(True, cypher, rows=rows)


def _render_value(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, list):
        return "[" + ", ".join(_render_value(item) for item in value) + "]"
    text = str(value)
    if len(text) > MAX_VALUE_CHARS:
        return text[:MAX_VALUE_CHARS] + "…"
    return text


def render_rows(rows: list[dict]) -> str:
    """Compact, readable rendering of query results."""
    if not rows:
        return "(no rows — the query ran successfully but matched nothing)"

    shown = rows[:MAX_ROWS_RENDERED]
    lines = [
        " | ".join(f"{key}={_render_value(value)}" for key, value in row.items())
        for row in shown
    ]
    if len(rows) > MAX_ROWS_RENDERED:
        lines.append(f"… {len(rows) - MAX_ROWS_RENDERED} more rows omitted")
    return "\n".join(lines)
