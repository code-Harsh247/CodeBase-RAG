"""Intermediate structures shared between language mappers and the resolver.

Mappers run per file and cannot know about definitions in other files, so every
cross-file reference (a call, a base class, a type annotation) is emitted as a
*pending* record. The resolver turns those into edges once the whole repository
has been parsed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from graph.schema import GraphFragment, NodeType


@dataclass
class ImportBinding:
    """A name bound into a module's namespace by an import statement."""

    local_name: str
    #: Module the name comes from; "" for a plain ``import x``.
    source_module: str
    #: Name as it exists in the source module (or the module path itself).
    original_name: str
    #: True for ``import x`` / ``import x as y`` (binds a module, not a member).
    is_module_import: bool
    node_id: str


@dataclass
class PendingCall:
    """A call site awaiting resolution to a Function/Method/Class node."""

    source_id: str
    module_qname: str
    #: Name being called: ``helper`` in ``helper()``, ``speak`` in ``d.speak()``.
    name: str
    #: Text left of the dot, or None for a plain call.
    receiver: str | None
    #: Qualified name of the class lexically enclosing the call site, if any.
    enclosing_class: str | None
    #: Qualified name of the function containing the call, for local var lookup.
    scope_key: str
    line: int


@dataclass
class PendingBase:
    """A base class expression awaiting resolution to a Class node."""

    class_id: str
    class_qname: str
    module_qname: str
    base_name: str


@dataclass
class PendingTypeRef:
    """A type annotation awaiting resolution to a Class node."""

    source_id: str
    module_qname: str
    type_name: str


@dataclass
class ModuleScope:
    """Everything the resolver needs to know about one parsed module."""

    qname: str
    node_id: str
    file_path: str
    #: True for ``__init__.py`` — relative imports resolve against it directly.
    is_package: bool = False
    #: Top-level name -> node id (classes and functions defined in this module).
    defs: dict[str, str] = field(default_factory=dict)
    def_types: dict[str, NodeType] = field(default_factory=dict)
    #: Local name -> binding.
    imports: dict[str, ImportBinding] = field(default_factory=dict)
    #: Class qualified name -> {method name -> node id}.
    class_methods: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Class qualified name -> node id.
    class_ids: dict[str, str] = field(default_factory=dict)
    #: Class qualified name -> raw base-class expressions.
    class_bases: dict[str, list[str]] = field(default_factory=dict)
    #: Function qualified name -> {variable -> class expression assigned to it}.
    local_var_types: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Enclosing function qualified name -> {name -> node id} for inner functions.
    nested_defs: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class ParsedModule:
    """Output of running a mapper over a single source file."""

    scope: ModuleScope
    fragment: GraphFragment = field(default_factory=GraphFragment)
    calls: list[PendingCall] = field(default_factory=list)
    bases: list[PendingBase] = field(default_factory=list)
    type_refs: list[PendingTypeRef] = field(default_factory=list)
