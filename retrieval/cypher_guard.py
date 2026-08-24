"""Validation for LLM-generated Cypher.

The model writes the queries, so nothing it produces is trusted. Two layers:
a static check for write clauses and repo scoping, then a server-side `EXPLAIN`
that parses and plans the query without running it.

Keyword scanning happens only after string literals and comments are stripped,
so a function named "delete_user" in a WHERE clause is not mistaken for a
DELETE clause.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Clauses that write, delete, or alter the database or its schema.
#:
#: Deliberately excluded: `IMPORT` and `START`. Neither is a write clause in
#: modern Cypher (CSV loading is caught by `LOAD`), and `IMPORT` collides with
#: the `Import` node label, which would reject every legitimate query about
#: imports. A guard that blocks valid reads fails as surely as one that permits
#: writes.
_WRITE_CLAUSES = (
    "CREATE",
    "MERGE",
    "DELETE",
    "DETACH",
    "SET",
    "REMOVE",
    "DROP",
    "FOREACH",
    "LOAD",
    "ALTER",
    "GRANT",
    "REVOKE",
    "TERMINATE",
)

_WRITE_PATTERN = re.compile(r"\b(" + "|".join(_WRITE_CLAUSES) + r")\b", re.IGNORECASE)

#: Procedures that can write or reach outside the database.
_UNSAFE_PROCEDURES = re.compile(
    r"\bCALL\s+(apoc\.(create|merge|refactor|trigger|load|periodic)|dbms|db\.index\.fulltext\.drop)",
    re.IGNORECASE,
)

_STRING_LITERAL = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|`(?:[^`\\]|\\.)*`")
_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


@dataclass
class Validation:
    ok: bool
    error: str | None = None

    def __bool__(self) -> bool:
        return self.ok


def strip_noise(query: str) -> str:
    """Blank out comments and string literals so keyword scanning is accurate."""
    without_comments = _BLOCK_COMMENT.sub(" ", _LINE_COMMENT.sub(" ", query))
    # Replace literals with spaces of no semantic content, preserving structure.
    return _STRING_LITERAL.sub(" '' ", without_comments)


def validate_cypher(query: str, require_repo_scope: bool = True) -> Validation:
    """Static safety checks. Returns a message the model can act on when invalid."""
    if not query or not query.strip():
        return Validation(False, "Query is empty.")

    scannable = strip_noise(query)

    write_match = _WRITE_PATTERN.search(scannable)
    if write_match:
        clause = write_match.group(1).upper()
        return Validation(
            False,
            f"Query contains the write clause {clause}. Only read-only queries "
            f"are allowed: use MATCH, WHERE, RETURN, ORDER BY, LIMIT.",
        )

    if _UNSAFE_PROCEDURES.search(scannable):
        return Validation(False, "Query calls a procedure that is not allowed.")

    if require_repo_scope and "$repo_id" not in scannable:
        return Validation(
            False,
            "Query must be scoped to the repository: match on {repo_id: $repo_id} "
            "for at least one node.",
        )

    return Validation(True)


def explain(client, query: str, params: dict) -> Validation:
    """Parse and plan the query server-side without executing it."""
    try:
        client.run(f"EXPLAIN {query}", **params)
    except Exception as exc:  # noqa: BLE001 - message is fed back to the model
        return Validation(False, f"Cypher error: {_first_line(exc)}")
    return Validation(True)


def _first_line(exc: Exception) -> str:
    message = getattr(exc, "message", None) or str(exc)
    return message.strip().splitlines()[0][:300]
