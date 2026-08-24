"""Render the graph schema as prompt text.

Generated from the enums and metadata in :mod:`graph.schema` so the description
the model sees cannot drift from the schema the ingester actually writes.
"""

from __future__ import annotations

from functools import cache

from graph.schema import (
    COMMON_PROPERTIES,
    EDGE_SEMANTICS,
    NODE_PROPERTIES,
    NODE_SEMANTICS,
    SHARED_LABEL,
    EdgeType,
    NodeType,
)


@cache
def schema_description() -> str:
    lines = [
        "GRAPH SCHEMA",
        "",
        f"Every node also carries the label `{SHARED_LABEL}` and these properties:",
        f"  {', '.join(COMMON_PROPERTIES)}",
        "",
        "NODE LABELS",
    ]

    for node_type in NodeType:
        extra = NODE_PROPERTIES.get(node_type, ())
        properties = f" (+ {', '.join(extra)})" if extra else ""
        lines.append(f"  ({node_type.value}){properties}")
        lines.append(f"      {NODE_SEMANTICS[node_type]}")

    lines += ["", "RELATIONSHIPS"]
    for edge_type in EdgeType:
        lines.append(f"  [:{edge_type.value}]")
        lines.append(f"      {EDGE_SEMANTICS[edge_type]}")

    lines += ["", "RULES", _RULES]
    return "\n".join(lines)


_RULES = f"""\
  - Read-only queries only: MATCH, WHERE, RETURN, ORDER BY, LIMIT, WITH,
    DISTINCT, count().
  - Always scope to the repository with {{repo_id: $repo_id}} on at least one
    matched node.
  - "Function" in a question almost always means "any callable". Match
    `(:Function|Method)` — or omit the label entirely — unless the user
    explicitly asks for standalone functions or for methods specifically.
    Filtering to `:Function` alone silently drops every method and produces a
    confidently wrong answer.
  - Never use `labels(n)[0]`: every node also carries the `{SHARED_LABEL}`
    label, which sorts first, so that expression returns `{SHARED_LABEL}` for
    everything. Use `[l IN labels(n) WHERE l <> '{SHARED_LABEL}'][0]`.
  - `qualified_name` is the dotted import path for Modules, Classes, Functions
    and Methods (e.g. `requests.sessions.Session.get`). Match on `name` instead
    when the user gives a bare identifier.
  - A method belongs to a class via [:DEFINES], not [:CONTAINS].
  - Return `file_path` and `start_line` alongside results so answers can cite
    source locations.
  - Always add a LIMIT (25 unless the question implies otherwise)."""
