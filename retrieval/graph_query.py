"""Execute validated Cypher and render results for the model to read."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from retrieval.cypher_guard import Validation, _first_line, explain, validate_cypher

#: Beyond this, rows are truncated — a wall of results crowds out the question.
MAX_ROWS_RENDERED = 40
MAX_VALUE_CHARS = 200
#: A single `collect(...)` can return a list of any length; cap it like rows.
MAX_LIST_ITEMS = 20


def _query_timeout() -> float:
    return float(os.environ.get("NEO4J_QUERY_TIMEOUT_SECONDS") or 5)


def _max_rows() -> int:
    return int(os.environ.get("NEO4J_MAX_ROWS") or 200)


@dataclass
class QueryOutcome:
    ok: bool
    cypher: str
    rows: list[dict] = field(default_factory=list)
    error: str | None = None
    #: More rows matched than were fetched.
    truncated: bool = False

    @property
    def row_count(self) -> int:
        return len(self.rows)


def _execute(client, cypher: str, params: dict) -> tuple[list[dict], bool]:
    """Read-only, time-boxed, row-capped where the client supports it."""
    run_read = getattr(client, "run_read", None)
    if run_read is None:  # test doubles
        rows = client.run(cypher, **params)
    else:
        rows = run_read(cypher, params, timeout_s=_query_timeout(), max_rows=_max_rows())
    limit = _max_rows()
    if len(rows) > limit:
        return rows[:limit], True
    return rows, False


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
        rows, truncated = _execute(client, cypher, params)
    except Exception as exc:  # noqa: BLE001 - surfaced to the model for a retry
        return QueryOutcome(False, cypher, error=f"Execution failed: {_first_line(exc)}")

    return QueryOutcome(True, cypher, rows=rows, truncated=truncated)


def _render_value(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, list):
        shown = ", ".join(_render_value(item) for item in value[:MAX_LIST_ITEMS])
        more = len(value) - MAX_LIST_ITEMS
        return f"[{shown}, …{more} more]" if more > 0 else f"[{shown}]"
    text = str(value)
    if len(text) > MAX_VALUE_CHARS:
        return text[:MAX_VALUE_CHARS] + "…"
    return text


def render_rows(rows: list[dict], truncated: bool = False) -> str:
    """Compact, readable rendering of query results."""
    if not rows:
        return "(no rows — the query ran successfully but matched nothing)"

    shown = rows[:MAX_ROWS_RENDERED]
    lines = [
        " | ".join(f"{key}={_render_value(value)}" for key, value in row.items())
        for row in shown
    ]
    if len(rows) > MAX_ROWS_RENDERED or truncated:
        more = f"at least {len(rows) - len(shown)}" if truncated else str(len(rows) - len(shown))
        lines.append(f"… {more} more rows omitted — add LIMIT or a tighter WHERE")
    return "\n".join(lines)
