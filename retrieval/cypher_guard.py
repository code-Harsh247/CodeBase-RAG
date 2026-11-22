"""Validation for LLM-generated Cypher.

The model writes the queries, so nothing it produces is trusted. Three layers:
a static check for write clauses, procedure calls and repo scoping; a
server-side `EXPLAIN` that parses and plans the query without running it; and,
at execution time, a READ-mode session with a transaction timeout
(`Neo4jClient.run_read`), so a write that somehow slipped past this module is
still refused by the database itself.

Keyword scanning happens only after string literals, quoted identifiers and
comments are blanked, so a function named "delete_user" in a WHERE clause is not
mistaken for a DELETE clause.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Clauses that write, delete, alter the database or its schema, or reach
#: across databases.
#:
#: Deliberately excluded: `IMPORT`, which collides with the `Import` node label
#: and would reject every legitimate query about imports. A guard that blocks
#: valid reads fails as surely as one that permits writes. (`STARTS WITH` is not
#: caught by `START`: the word boundary needs a non-word character after it.)
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
    "SHOW",
    "USE",
    "STOP",
    "START",
    "TRANSACTIONS",
)

_WRITE_PATTERN = re.compile(r"\b(" + "|".join(_WRITE_CLAUSES) + r")\b", re.IGNORECASE)

#: Every procedure call is refused, not just a list of known-dangerous ones.
#: The agent never needs one, and a denylist kept losing: `apoc.cypher.doIt`
#: runs a write hidden inside a string literal, and backtick-quoted names slip
#: past any pattern that expects a dotted procedure name.
_CALL = re.compile(r"\bCALL\b", re.IGNORECASE)

#: String literals, quoted identifiers and comments, matched in ONE left-to-right
#: pass so whichever starts first wins. Stripping comments before strings (as an
#: earlier version did) let `WHERE n.name = '//' DETACH DELETE n` hide its write:
#: the `//` inside the string was taken for a comment and ate the rest of the line.
_NOISE = re.compile(
    r"'(?:[^'\\]|\\.)*'"
    r'|"(?:[^"\\]|\\.)*"'
    r"|`(?:[^`]|``)*`"
    r"|//[^\n]*"
    r"|/\*.*?\*/",
    re.DOTALL,
)

#: Placeholder for a blanked literal. Contains no quote character, so anything
#: quote-like left after stripping is an unterminated literal or comment.
_LITERAL = " _lit_ "

#: The only ways `repo_id` may appear: as the scoping map entry, or as an
#: equality against the bound parameter. Anything else (`RETURN n.repo_id`,
#: `n.repo_id <> $repo_id`) could read across repositories.
_SCOPED_REPO_ID = re.compile(
    r"\brepo_id\s*:\s*\$repo_id\b"
    r"|\.\s*repo_id\s*=\s*\$repo_id\b"
    r"|\$repo_id\s*=\s*\w+\s*\.\s*repo_id\b"
)
_ANY_REPO_ID = re.compile(r"\brepo_id\b")


@dataclass
class Validation:
    ok: bool
    error: str | None = None

    def __bool__(self) -> bool:
        return self.ok


def _blank(match: re.Match) -> str:
    return " " if match.group(0)[:2] in ("//", "/*") else _LITERAL


def strip_noise(query: str) -> str:
    """Blank out comments, string literals and quoted identifiers."""
    return _NOISE.sub(_blank, query)


def validate_cypher(query: str, require_repo_scope: bool = True) -> Validation:
    """Static safety checks. Returns a message the model can act on when invalid."""
    if not query or not query.strip():
        return Validation(False, "Query is empty.")

    scannable = strip_noise(query)

    # Anything quote-like that survived stripping never closed. Refuse rather
    # than guess where it was meant to end.
    if any(token in scannable for token in ("'", '"', "`", "/*")):
        return Validation(False, "Query has an unterminated string, identifier or comment.")

    write_match = _WRITE_PATTERN.search(scannable)
    if write_match:
        clause = write_match.group(1).upper()
        return Validation(
            False,
            f"Query contains the clause {clause}. Only read-only queries "
            f"are allowed: use MATCH, WHERE, RETURN, ORDER BY, LIMIT.",
        )

    if _CALL.search(scannable):
        return Validation(False, "Procedure calls (CALL) are not allowed.")

    scoped = _SCOPED_REPO_ID.findall(scannable)
    if _ANY_REPO_ID.search(_SCOPED_REPO_ID.sub(" ", scannable)):
        return Validation(
            False,
            "repo_id may only be used to scope the query: {repo_id: $repo_id} or "
            "n.repo_id = $repo_id. Do not return it or compare it to anything else.",
        )

    if require_repo_scope and not scoped:
        return Validation(
            False,
            "Query must be scoped to the repository: match on {repo_id: $repo_id} "
            "for at least one node.",
        )

    return Validation(True)


def explain(client, query: str, params: dict) -> Validation:
    """Parse and plan the query server-side without executing it."""
    try:
        run = getattr(client, "run_read", None)
        if run is not None:
            run(f"EXPLAIN {query}", params)
        else:
            client.run(f"EXPLAIN {query}", **params)
    except Exception as exc:  # noqa: BLE001 - message is fed back to the model
        return Validation(False, f"Cypher error: {_first_line(exc)}")
    return Validation(True)


def _first_line(exc: Exception) -> str:
    message = getattr(exc, "message", None) or str(exc)
    return message.strip().splitlines()[0][:300]
