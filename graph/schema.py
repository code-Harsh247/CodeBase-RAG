"""Graph schema: node/edge types, in-memory records, and deterministic IDs.

The schema is intentionally language-agnostic (see docs/ARCHITECTURE.md). Each
language gets its own mapper that emits these same node and edge types.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum

# Every node also carries this label so a single index serves all id lookups.
SHARED_LABEL = "CodeNode"


class NodeType(str, Enum):
    FILE = "File"
    MODULE = "Module"
    CLASS = "Class"
    FUNCTION = "Function"
    METHOD = "Method"
    IMPORT = "Import"


class EdgeType(str, Enum):
    CONTAINS = "CONTAINS"
    DEFINES = "DEFINES"
    CALLS = "CALLS"
    IMPORTS = "IMPORTS"
    INHERITS = "INHERITS"
    REFERENCES = "REFERENCES"


#: Node types that can appear as the source of a CALLS edge.
CALLABLE_TYPES = (NodeType.FUNCTION, NodeType.METHOD)


def make_node_id(repo_id: str, file_path: str, qualified_name: str, node_type: NodeType) -> str:
    """Deterministic node ID so re-ingesting the same commit is idempotent."""
    raw = f"{repo_id}\x00{file_path}\x00{qualified_name}\x00{node_type.value}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@dataclass
class Node:
    id: str
    type: NodeType
    properties: dict = field(default_factory=dict)


@dataclass
class Edge:
    source_id: str
    target_id: str
    type: EdgeType
    properties: dict = field(default_factory=dict)


@dataclass
class GraphFragment:
    """Nodes and edges produced from one file, before cross-file resolution."""

    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)

    def extend(self, other: GraphFragment) -> None:
        self.nodes.extend(other.nodes)
        self.edges.extend(other.edges)
