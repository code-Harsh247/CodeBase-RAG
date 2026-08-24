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

#: Properties every node carries, regardless of type.
COMMON_PROPERTIES: tuple[str, ...] = ("id", "repo_id", "qualified_name", "file_path", "name")

#: Type-specific properties, beyond COMMON_PROPERTIES. Kept here next to the
#: enums so the prompt sent to the LLM is generated from the schema definition
#: rather than hand-maintained in a second place.
NODE_PROPERTIES: dict[NodeType, tuple[str, ...]] = {
    NodeType.FILE: ("language",),
    NodeType.MODULE: (),
    NodeType.CLASS: ("start_line", "end_line"),
    NodeType.FUNCTION: ("signature", "start_line", "end_line"),
    NodeType.METHOD: ("signature", "start_line", "end_line"),
    NodeType.IMPORT: ("source_module", "imported_name", "alias", "start_line", "is_internal"),
}

#: What each node type represents, in the terms a question would use.
NODE_SEMANTICS: dict[NodeType, str] = {
    NodeType.FILE: "A source file on disk. `qualified_name` is its repo-relative path.",
    NodeType.MODULE: (
        "An importable module. `qualified_name` is the dotted import name "
        "(e.g. `requests.sessions`), not the file path."
    ),
    NodeType.CLASS: "A class definition.",
    NodeType.FUNCTION: "A module-level or nested function (not attached to a class).",
    NodeType.METHOD: "A function defined inside a class body.",
    NodeType.IMPORT: (
        "A name bound by an import statement. `is_internal` is true when it "
        "resolves to a module inside this repository."
    ),
}

#: Direction and meaning of each relationship, with its properties.
EDGE_SEMANTICS: dict[EdgeType, str] = {
    EdgeType.CONTAINS: (
        "(File)->(Module), (Module)->(Class|Function), and nesting: "
        "(Class)->(Class), (Function)->(Function)."
    ),
    EdgeType.DEFINES: "(Class)->(Method). A class defines its methods.",
    EdgeType.CALLS: (
        "(Function|Method)->(Function|Method). The source calls the target. "
        "Properties: `line` (first call site), `occurrences` (if called more than once)."
    ),
    EdgeType.IMPORTS: (
        "(Module)->(Import), then (Import)->(Module) when the import resolves "
        "to an internal module. Traverse both hops to link importer to imported."
    ),
    EdgeType.INHERITS: "(Class)->(Class). The source subclasses the target.",
    EdgeType.REFERENCES: (
        "(Function|Method)->(Class). The source instantiates the class or "
        "names it in a type annotation."
    ),
}


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
