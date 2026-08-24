"""Resolve cross-file references into graph edges.

Mappers emit *pending* references because a single file cannot know what other
files define. This module runs once the whole repository is parsed and turns
those pending records into CALLS / INHERITS / REFERENCES / IMPORTS edges.

Known limitations (measured by the eval harness rather than hidden):

* Attribute calls resolve only when the receiver is ``self``/``cls``, a local
  variable assigned directly from a constructor, an imported module, or a class
  name. Anything else falls back to a globally-unique-name heuristic.
* Dynamic dispatch, ``getattr``, monkey-patching and star-imports are not
  tracked.
* The unique-name fallback links a call only when exactly one candidate exists
  repository-wide, trading recall for precision.
"""

from __future__ import annotations

import builtins
from collections import defaultdict
from dataclasses import dataclass, field

from graph.schema import Edge, EdgeType, NodeType
from ingestion.symbols import ModuleScope, ParsedModule

#: Guard against import cycles when following re-exports.
_MAX_REEXPORT_DEPTH = 4

#: Names that can never resolve to a repository definition.
_BUILTIN_NAMES = frozenset(dir(builtins)) | {
    "Any",
    "Callable",
    "ClassVar",
    "Dict",
    "Iterable",
    "Iterator",
    "List",
    "Literal",
    "Mapping",
    "Optional",
    "Self",
    "Sequence",
    "Set",
    "Tuple",
    "Union",
}


@dataclass
class ResolutionStats:
    """Counts split three ways so the numbers mean something.

    A reference is *external* when it provably points outside the repository (a
    non-internal import, or a builtin). Those can never become edges, so folding
    them into "unresolved" would understate resolution quality. What is left —
    *unresolved* — is the honest measure of what the resolver misses.
    """

    calls_total: int = 0
    calls_resolved: int = 0
    calls_external: int = 0
    #: Attribute calls whose receiver has no inferable type — overwhelmingly
    #: builtins (``kwargs.get``, ``text.strip``), which have no node to link to.
    calls_unknown_receiver: int = 0
    bases_total: int = 0
    bases_resolved: int = 0
    bases_external: int = 0
    type_refs_total: int = 0
    type_refs_resolved: int = 0
    type_refs_external: int = 0
    imports_total: int = 0
    imports_internal: int = 0

    def as_dict(self) -> dict[str, int]:
        return self.__dict__.copy()


@dataclass
class ResolutionResult:
    edges: list[Edge] = field(default_factory=list)
    #: node id -> property updates to apply before loading.
    node_updates: dict[str, dict] = field(default_factory=dict)
    stats: ResolutionStats = field(default_factory=ResolutionStats)


class Resolver:
    """Resolves pending references across all parsed modules of one repository."""

    def __init__(self, modules: list[ParsedModule]) -> None:
        self.modules = modules
        self.scopes = {module.scope.qname: module.scope for module in modules}
        self.result = ResolutionResult()

        #: Simple name -> [(node_id, NodeType)] for module-level definitions.
        self.defs_by_name: dict[str, list[tuple[str, NodeType]]] = defaultdict(list)
        #: Method name -> [(class_qname, node_id)].
        self.methods_by_name: dict[str, list[tuple[str, str]]] = defaultdict(list)
        #: Class qualified name -> node id, across the whole repo.
        self.class_ids: dict[str, str] = {}
        #: Node id -> class qualified name, to go back from a lookup to a class.
        self.class_qname_by_id: dict[str, str] = {}
        #: Class qualified name -> resolved internal base class qnames.
        self.class_bases: dict[str, list[str]] = {}
        #: Module qualified name -> {method name -> node id} per class.
        self.class_methods: dict[str, dict[str, str]] = {}
        #: Import node ids that were resolved to an internal module.
        self._internal_imports: set[str] = set()

        self._build_indexes()

    # --------------------------------------------------------------- indexes

    def _build_indexes(self) -> None:
        for module in self.modules:
            scope = module.scope
            for name, node_id in scope.defs.items():
                self.defs_by_name[name].append((node_id, scope.def_types[name]))
            for class_qname, node_id in scope.class_ids.items():
                self.class_ids[class_qname] = node_id
                self.class_qname_by_id[node_id] = class_qname
            for class_qname, methods in scope.class_methods.items():
                self.class_methods[class_qname] = methods
                for method_name, method_id in methods.items():
                    self.methods_by_name[method_name].append((class_qname, method_id))

    # ------------------------------------------------------------------ main

    def resolve(self) -> ResolutionResult:
        self._resolve_imports()
        self._resolve_bases()
        self._resolve_calls()
        self._resolve_type_refs()
        return self.result

    # --------------------------------------------------------------- modules

    def _resolve_module(self, source_module: str, from_module: str) -> str | None:
        """Map an import's source module onto an internal module qualified name."""
        if not source_module:
            return None
        if source_module.startswith("."):
            source_module = self._resolve_relative(source_module, from_module)
            if source_module is None:
                return None
        if source_module in self.scopes:
            return source_module
        suffix = "." + source_module
        matches = [qname for qname in self.scopes if qname.endswith(suffix)]
        return matches[0] if len(matches) == 1 else None

    def _resolve_relative(self, source_module: str, from_module: str) -> str | None:
        """``from .rel import x`` inside ``pkg.mod`` -> ``pkg.rel``."""
        level = len(source_module) - len(source_module.lstrip("."))
        remainder = source_module[level:]

        parts = from_module.split(".")
        scope = self.scopes.get(from_module)
        if not (scope is not None and scope.is_package):
            # A module resolves relative imports against its containing package.
            parts = parts[:-1]

        # One dot is the current package; each extra dot walks one level up.
        up = level - 1
        if up > len(parts):
            return None
        base = parts[: len(parts) - up] if up else parts
        if remainder:
            base = base + remainder.split(".")
        return ".".join(base) if base else None

    def _is_external(self, scope: ModuleScope, name: str) -> bool:
        """True when ``name`` provably comes from outside the repository."""
        head = name.split(".", 1)[0]
        if head in _BUILTIN_NAMES:
            return True
        binding = scope.imports.get(head)
        return binding is not None and binding.node_id not in self._internal_imports

    def _module_member(
        self, module_qname: str, name: str, depth: int = 0
    ) -> tuple[str, NodeType] | None:
        """Find ``name`` defined in ``module_qname``, following re-exports."""
        scope = self.scopes.get(module_qname)
        if scope is None:
            return None
        if name in scope.defs:
            return scope.defs[name], scope.def_types[name]
        binding = scope.imports.get(name)
        if binding is not None and not binding.is_module_import and depth < _MAX_REEXPORT_DEPTH:
            target = self._resolve_module(binding.source_module, module_qname)
            if target is not None:
                return self._module_member(target, binding.original_name, depth + 1)
        return None

    # --------------------------------------------------------------- imports

    def _resolve_imports(self) -> None:
        for module in self.modules:
            scope = module.scope
            for binding in scope.imports.values():
                self.result.stats.imports_total += 1
                source = binding.original_name if binding.is_module_import else binding.source_module
                target_module = self._resolve_module(source, scope.qname)
                if target_module is None:
                    continue
                self.result.stats.imports_internal += 1
                self._internal_imports.add(binding.node_id)
                self.result.node_updates.setdefault(binding.node_id, {})["is_internal"] = True
                self.result.edges.append(
                    Edge(binding.node_id, self.scopes[target_module].node_id, EdgeType.IMPORTS)
                )

    # ----------------------------------------------------------- inheritance

    def _resolve_bases(self) -> None:
        resolved_bases: dict[str, list[str]] = defaultdict(list)
        for module in self.modules:
            for pending in module.bases:
                self.result.stats.bases_total += 1
                found = self._lookup_name(pending.module_qname, pending.base_name)
                if found is None or found[1] is not NodeType.CLASS:
                    if self._is_external(module.scope, pending.base_name):
                        self.result.stats.bases_external += 1
                    continue
                base_id = found[0]
                base_qname = self.class_qname_by_id.get(base_id)
                if base_qname is None:
                    continue
                self.result.stats.bases_resolved += 1
                resolved_bases[pending.class_qname].append(base_qname)
                self.result.edges.append(Edge(pending.class_id, base_id, EdgeType.INHERITS))
        self.class_bases = dict(resolved_bases)

    # ----------------------------------------------------------------- names

    def _lookup_name(self, module_qname: str, name: str) -> tuple[str, NodeType] | None:
        """Resolve a (possibly dotted) name as seen from ``module_qname``."""
        scope = self.scopes.get(module_qname)
        if scope is None:
            return None

        if "." in name:
            head, _, tail = name.partition(".")
            binding = scope.imports.get(head)
            if binding is not None and binding.is_module_import:
                target = self._resolve_module(binding.original_name, module_qname)
                if target is not None:
                    return self._module_member(target, tail)
            return None

        if name in scope.defs:
            return scope.defs[name], scope.def_types[name]

        binding = scope.imports.get(name)
        if binding is not None and not binding.is_module_import:
            target = self._resolve_module(binding.source_module, module_qname)
            if target is not None:
                found = self._module_member(target, binding.original_name)
                if found is not None:
                    return found

        candidates = self.defs_by_name.get(name, [])
        return candidates[0] if len(candidates) == 1 else None

    def _find_method(self, class_qname: str, method_name: str, depth: int = 0) -> str | None:
        """Look up a method on a class, walking resolved base classes."""
        methods = self.class_methods.get(class_qname)
        if methods and method_name in methods:
            return methods[method_name]
        if depth >= _MAX_REEXPORT_DEPTH:
            return None
        for base in self.class_bases.get(class_qname, []):
            found = self._find_method(base, method_name, depth + 1)
            if found is not None:
                return found
        return None

    # ----------------------------------------------------------------- calls

    def _resolve_calls(self) -> None:
        for module in self.modules:
            for pending in module.calls:
                self.result.stats.calls_total += 1
                edge = self._resolve_call(module, pending)
                if edge is not None:
                    self.result.stats.calls_resolved += 1
                    self.result.edges.append(edge)
                elif self._is_external(module.scope, pending.receiver or pending.name):
                    self.result.stats.calls_external += 1
                elif pending.receiver is not None and not self._receiver_is_known(module, pending):
                    self.result.stats.calls_unknown_receiver += 1

    def _receiver_is_known(self, module: ParsedModule, pending) -> bool:
        """True when we know what the receiver *is*, even if the method is missing."""
        scope = module.scope
        receiver = pending.receiver
        if receiver in ("self", "cls", "super()") and pending.enclosing_class:
            return True
        if receiver in scope.local_var_types.get(pending.scope_key, {}):
            return True
        if self._lookup_name(pending.module_qname, receiver) is not None:
            return True
        binding = scope.imports.get(receiver.split(".", 1)[0])
        return binding is not None and binding.node_id in self._internal_imports

    def _resolve_call(self, module: ParsedModule, pending) -> Edge | None:
        target = self._call_target(module, pending)
        if target is None:
            return None
        target_id, target_type = target
        if target_id == pending.source_id:
            return None
        # Calling a class constructs an instance: that is a reference, not a call.
        edge_type = EdgeType.REFERENCES if target_type is NodeType.CLASS else EdgeType.CALLS
        return Edge(pending.source_id, target_id, edge_type, {"line": pending.line})

    @staticmethod
    def _enclosing_scopes(scope_key: str) -> list[str]:
        """``a.b.c`` -> ``[a.b.c, a.b, a]`` so inner functions see their siblings."""
        parts = scope_key.split(".")
        return [".".join(parts[:index]) for index in range(len(parts), 0, -1)]

    def _call_target(self, module: ParsedModule, pending) -> tuple[str, NodeType] | None:
        scope = module.scope

        if pending.receiver is None:
            # An inner function shadows anything at module level.
            for enclosing in self._enclosing_scopes(pending.scope_key):
                nested = scope.nested_defs.get(enclosing, {})
                if pending.name in nested:
                    return nested[pending.name], NodeType.FUNCTION
            return self._lookup_name(pending.module_qname, pending.name)

        # self.foo() / cls.foo() inside a class body.
        if pending.receiver in ("self", "cls") and pending.enclosing_class:
            method_id = self._find_method(pending.enclosing_class, pending.name)
            return (method_id, NodeType.METHOD) if method_id else None

        # super().foo() resolves against base classes only, skipping the override.
        if pending.receiver == "super()" and pending.enclosing_class:
            for base in self.class_bases.get(pending.enclosing_class, []):
                method_id = self._find_method(base, pending.name)
                if method_id:
                    return method_id, NodeType.METHOD
            return None

        # x = SomeClass(); x.foo()
        local_types = scope.local_var_types.get(pending.scope_key, {})
        class_expr = local_types.get(pending.receiver)
        if class_expr is not None:
            found = self._lookup_name(pending.module_qname, class_expr)
            if found is not None and found[1] is NodeType.CLASS:
                class_qname = self.class_qname_by_id.get(found[0])
                if class_qname:
                    method_id = self._find_method(class_qname, pending.name)
                    if method_id:
                        return method_id, NodeType.METHOD

        # SomeClass.foo() or module.foo()
        found = self._lookup_name(pending.module_qname, pending.receiver)
        if found is not None and found[1] is NodeType.CLASS:
            class_qname = self.class_qname_by_id.get(found[0])
            if class_qname:
                method_id = self._find_method(class_qname, pending.name)
                if method_id:
                    return method_id, NodeType.METHOD
        binding = scope.imports.get(pending.receiver)
        if binding is not None and binding.is_module_import:
            target_module = self._resolve_module(binding.original_name, pending.module_qname)
            if target_module is not None:
                member = self._module_member(target_module, pending.name)
                if member is not None:
                    return member

        # Last resort: a method name that is unique across the repository.
        candidates = self.methods_by_name.get(pending.name, [])
        if len(candidates) == 1:
            return candidates[0][1], NodeType.METHOD
        return None

    # ------------------------------------------------------------ type hints

    def _resolve_type_refs(self) -> None:
        seen: set[tuple[str, str]] = set()
        for module in self.modules:
            for pending in module.type_refs:
                self.result.stats.type_refs_total += 1
                found = self._lookup_name(pending.module_qname, pending.type_name)
                if found is None or found[1] is not NodeType.CLASS:
                    if self._is_external(module.scope, pending.type_name):
                        self.result.stats.type_refs_external += 1
                    continue
                key = (pending.source_id, found[0])
                if key in seen:
                    continue
                seen.add(key)
                self.result.stats.type_refs_resolved += 1
                self.result.edges.append(Edge(pending.source_id, found[0], EdgeType.REFERENCES))
