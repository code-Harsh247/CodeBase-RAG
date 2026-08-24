"""Map a Python tree-sitter AST onto the shared graph schema.

Emits definition nodes and containment edges directly; every reference that may
point at another file (calls, base classes, annotations) is emitted as a pending
record for :mod:`ingestion.resolver`.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from graph.schema import Edge, EdgeType, Node, NodeType, make_node_id
from ingestion.symbols import (
    ImportBinding,
    ModuleScope,
    ParsedModule,
    PendingBase,
    PendingCall,
    PendingTypeRef,
)

#: Definition node types that open a new lexical scope.
_DEFINITION_TYPES = frozenset({"function_definition", "class_definition"})


def module_qname_for(repo_root: Path, file_path: Path) -> str:
    """The name a module is imported by, not its path.

    The package root is the outermost unbroken run of directories containing
    ``__init__.py``, so a ``src/`` layout yields ``requests.api`` rather than
    ``src.requests.api`` — which is what someone asking about the code will say.
    """
    parts = list(file_path.relative_to(repo_root).parts)
    stem = PurePosixPath(parts.pop()).stem

    package_parts: list[str] = []
    for depth in range(len(parts), 0, -1):
        if not (repo_root.joinpath(*parts[:depth]) / "__init__.py").exists():
            break
        package_parts.insert(0, parts[depth - 1])

    if stem == "__init__":
        return ".".join(package_parts)
    return ".".join([*package_parts, stem])


def _child_definitions(node, is_overload=None) -> list:
    """Definitions directly owned by ``node``, not nested inside another definition.

    ``@typing.overload`` stubs are skipped. They declare extra type signatures
    for a function that is defined again below them, so emitting each one would
    produce several nodes sharing a qualified name for what is, to anyone asking
    about the code, a single function.
    """
    found: list = []
    queue = list(node.children)
    while queue:
        current = queue.pop(0)
        if current.type == "decorated_definition":
            inner = current.child_by_field_name("definition")
            if inner is not None and not (is_overload and is_overload(current)):
                found.append(inner)
            continue
        if current.type in _DEFINITION_TYPES:
            found.append(current)
            continue
        queue.extend(current.children)
    return found


class PythonMapper:
    """Walks one Python file and produces a :class:`ParsedModule`."""

    def __init__(self, repo_id: str, source: bytes, rel_path: str, module_qname: str) -> None:
        self.repo_id = repo_id
        self.source = source
        self.rel_path = rel_path
        self.module_qname = module_qname
        self.result = ParsedModule(
            scope=ModuleScope(
                qname=self.module_qname,
                node_id="",
                file_path=rel_path,
                is_package=PurePosixPath(rel_path).name == "__init__.py",
            )
        )

    # ---------------------------------------------------------------- helpers

    def _text(self, node) -> str:
        return self.source[node.start_byte : node.end_byte].decode("utf-8", "replace")

    def _is_overload_stub(self, decorated) -> bool:
        """True when a decorated definition carries @overload / @typing.overload."""
        for child in decorated.children:
            if child.type != "decorator":
                continue
            name = self._text(child).lstrip("@").split("(")[0].strip()
            if name.rsplit(".", 1)[-1] == "overload":
                return True
        return False

    def _definitions_in(self, node) -> list:
        return _child_definitions(node, self._is_overload_stub)

    def _docstring(self, body) -> str | None:
        """The leading string literal of a class or function body, if present.

        This is the only natural-language description of intent the source
        carries, so it is what semantic search embeds.
        """
        if body is None:
            return None
        first = next((child for child in body.named_children), None)
        if first is None or first.type != "expression_statement":
            return None
        literal = next((child for child in first.named_children), None)
        if literal is None or literal.type != "string":
            return None

        # The grammar separates string_start/content/end, so prefixes like r"""
        # and the quotes themselves never reach the text.
        content = next(
            (child for child in literal.children if child.type == "string_content"), None
        )
        if content is None:
            return None
        return " ".join(self._text(content).split()) or None

    @staticmethod
    def _collapse(text: str) -> str:
        """One-line the text, closing up the gaps a wrapped/trailing comma leaves."""
        collapsed = " ".join(text.split())
        collapsed = re.sub(r"\(\s+", "(", collapsed)
        return re.sub(r",?\s+\)", ")", collapsed)

    def _add_node(self, node_type: NodeType, qualified_name: str, **props) -> str:
        node_id = make_node_id(self.repo_id, self.rel_path, qualified_name, node_type)
        self.result.fragment.nodes.append(
            Node(
                id=node_id,
                type=node_type,
                properties={
                    "repo_id": self.repo_id,
                    "qualified_name": qualified_name,
                    "file_path": self.rel_path,
                    **props,
                },
            )
        )
        return node_id

    def _add_edge(self, source_id: str, target_id: str, edge_type: EdgeType, **props) -> None:
        self.result.fragment.edges.append(Edge(source_id, target_id, edge_type, props))

    # ------------------------------------------------------------------- run

    def run(self, tree) -> ParsedModule:
        scope = self.result.scope

        file_id = self._add_node(
            NodeType.FILE, self.rel_path, name=PurePosixPath(self.rel_path).name, language="python"
        )
        module_id = self._add_node(
            NodeType.MODULE, self.module_qname, name=self.module_qname.rsplit(".", 1)[-1]
        )
        scope.node_id = module_id
        self._add_edge(file_id, module_id, EdgeType.CONTAINS)

        root = tree.root_node
        for statement in self._module_level_imports(root):
            self._handle_import(statement, module_id)

        for definition in self._definitions_in(root):
            if definition.type == "class_definition":
                self._handle_class(definition, self.module_qname, module_id, EdgeType.CONTAINS)
            elif definition.type == "function_definition":
                self._handle_function(
                    definition,
                    parent_qname=self.module_qname,
                    container_id=module_id,
                    node_type=NodeType.FUNCTION,
                    containment=EdgeType.CONTAINS,
                    enclosing_class=None,
                )

        return self.result

    # --------------------------------------------------------------- imports

    @staticmethod
    def _module_level_imports(root) -> list:
        """Every import that binds a module-level name.

        Real code routinely guards imports with ``if TYPE_CHECKING:`` or
        ``try/except ImportError``, so scanning only the root's direct children
        misses them. Function and class bodies are skipped: those imports are
        local, and hoisting them would pollute the module namespace.
        """
        found: list = []
        queue = list(root.children)
        while queue:
            current = queue.pop(0)
            if current.type in _DEFINITION_TYPES or current.type == "decorated_definition":
                continue
            if current.type in ("import_statement", "import_from_statement"):
                found.append(current)
                continue
            queue.extend(current.children)
        return found

    def _handle_import(self, node, module_id: str) -> None:
        if node.type == "import_statement":
            for child in node.children_by_field_name("name"):
                if child.type == "aliased_import":
                    target = self._text(child.child_by_field_name("name"))
                    alias = self._text(child.child_by_field_name("alias"))
                    self._record_import(node, module_id, alias, "", target, True, alias)
                elif child.type == "dotted_name":
                    target = self._text(child)
                    # `import a.b` binds the top-level name `a`.
                    self._record_import(node, module_id, target.split(".")[0], "", target, True, None)
            return

        module_node = node.child_by_field_name("module_name")
        source_module = self._text(module_node) if module_node is not None else ""
        for child in node.children_by_field_name("name"):
            if child.type == "aliased_import":
                original = self._text(child.child_by_field_name("name"))
                alias = self._text(child.child_by_field_name("alias"))
                self._record_import(node, module_id, alias, source_module, original, False, alias)
            elif child.type == "dotted_name":
                original = self._text(child)
                self._record_import(node, module_id, original, source_module, original, False, None)

    def _record_import(
        self,
        node,
        module_id: str,
        local_name: str,
        source_module: str,
        original_name: str,
        is_module_import: bool,
        alias: str | None,
    ) -> None:
        line = node.start_point[0] + 1
        key = f"{self.module_qname}:import:{local_name}:{line}"
        import_id = self._add_node(
            NodeType.IMPORT,
            key,
            name=local_name,
            source_module=source_module,
            imported_name=original_name,
            alias=alias,
            start_line=line,
            # Filled in by the resolver once internal modules are known.
            is_internal=False,
        )
        self._add_edge(module_id, import_id, EdgeType.IMPORTS)
        self.result.scope.imports[local_name] = ImportBinding(
            local_name=local_name,
            source_module=source_module,
            original_name=original_name,
            is_module_import=is_module_import,
            node_id=import_id,
        )

    # --------------------------------------------------------------- classes

    def _handle_class(
        self, node, parent_qname: str, container_id: str, containment: EdgeType
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._text(name_node)
        qname = f"{parent_qname}.{name}"
        scope = self.result.scope

        class_id = self._add_node(
            NodeType.CLASS,
            qname,
            name=name,
            docstring=self._docstring(node.child_by_field_name("body")),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
        )
        self._add_edge(container_id, class_id, containment)

        if parent_qname == self.module_qname:
            scope.defs[name] = class_id
            scope.def_types[name] = NodeType.CLASS
        scope.class_ids[qname] = class_id
        scope.class_methods.setdefault(qname, {})

        superclasses = node.child_by_field_name("superclasses")
        bases: list[str] = []
        if superclasses is not None:
            for child in superclasses.named_children:
                base_text = self._text(child)
                bases.append(base_text)
                self.result.bases.append(
                    PendingBase(
                        class_id=class_id,
                        class_qname=qname,
                        module_qname=self.module_qname,
                        base_name=base_text,
                    )
                )
        scope.class_bases[qname] = bases

        body = node.child_by_field_name("body")
        if body is None:
            return
        for definition in self._definitions_in(body):
            if definition.type == "function_definition":
                method_id = self._handle_function(
                    definition,
                    parent_qname=qname,
                    container_id=class_id,
                    node_type=NodeType.METHOD,
                    containment=EdgeType.DEFINES,
                    enclosing_class=qname,
                )
                method_name_node = definition.child_by_field_name("name")
                if method_id and method_name_node is not None:
                    scope.class_methods[qname][self._text(method_name_node)] = method_id
            elif definition.type == "class_definition":
                self._handle_class(definition, qname, class_id, EdgeType.CONTAINS)

    # ------------------------------------------------------------- functions

    def _handle_function(
        self,
        node,
        parent_qname: str,
        container_id: str,
        node_type: NodeType,
        containment: EdgeType,
        enclosing_class: str | None,
    ) -> str | None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = self._text(name_node)
        qname = f"{parent_qname}.{name}"
        scope = self.result.scope

        params_node = node.child_by_field_name("parameters")
        return_node = node.child_by_field_name("return_type")
        # Collapse wrapped parameter lists onto one line so signatures stay
        # readable in query output and compact in LLM prompts.
        signature = self._collapse(self._text(params_node)) if params_node is not None else "()"
        if return_node is not None:
            signature += f" -> {self._collapse(self._text(return_node))}"

        func_id = self._add_node(
            node_type,
            qname,
            name=name,
            signature=signature,
            docstring=self._docstring(node.child_by_field_name("body")),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
        )
        self._add_edge(container_id, func_id, containment)

        if node_type is NodeType.FUNCTION:
            if parent_qname == self.module_qname:
                scope.defs[name] = func_id
                scope.def_types[name] = NodeType.FUNCTION
            else:
                # Inner function: visible to its siblings and to the enclosing body.
                scope.nested_defs.setdefault(parent_qname, {})[name] = func_id

        for annotation in (params_node, return_node):
            if annotation is not None:
                self._record_type_refs(annotation, func_id)
        if params_node is not None:
            self._record_param_types(params_node, qname)

        body = node.child_by_field_name("body")
        if body is not None:
            self._walk_body(body, func_id, qname, enclosing_class)
            for definition in self._definitions_in(body):
                if definition.type == "function_definition":
                    self._handle_function(
                        definition,
                        parent_qname=qname,
                        container_id=func_id,
                        node_type=NodeType.FUNCTION,
                        containment=EdgeType.CONTAINS,
                        enclosing_class=enclosing_class,
                    )
                elif definition.type == "class_definition":
                    self._handle_class(definition, qname, func_id, EdgeType.CONTAINS)

        return func_id

    def _record_param_types(self, params_node, owner_qname: str) -> None:
        """Treat ``def f(x: Foo)`` as typing ``x``, so ``x.bar()`` can resolve."""
        for param in params_node.named_children:
            if param.type not in ("typed_parameter", "typed_default_parameter"):
                continue
            type_node = param.child_by_field_name("type")
            name_node = next(
                (child for child in param.named_children if child.type == "identifier"), None
            )
            if type_node is None or name_node is None:
                continue
            type_text = self._text(type_node)
            # Only plain class names; generics like list[Foo] are left unresolved.
            if not type_text.isidentifier():
                continue
            self.result.scope.local_var_types.setdefault(owner_qname, {}).setdefault(
                self._text(name_node), type_text
            )

    def _record_type_refs(self, node, source_id: str) -> None:
        # ``include_self`` matters for return annotations: the node handed in is
        # itself the ``type`` node, whereas parameters wrap theirs.
        for type_node in self._descendants_of_type(node, "type", include_self=True):
            for identifier in self._descendants_of_type(type_node, "identifier", include_self=True):
                self.result.type_refs.append(
                    PendingTypeRef(
                        source_id=source_id,
                        module_qname=self.module_qname,
                        type_name=self._text(identifier),
                    )
                )

    @staticmethod
    def _descendants_of_type(node, node_type: str, include_self: bool = False) -> list:
        found = []
        queue = [node] if include_self else list(node.children)
        while queue:
            current = queue.pop(0)
            if current.type == node_type:
                found.append(current)
            queue.extend(current.children)
        return found

    # ------------------------------------------------------------ call sites

    def _walk_body(self, body, owner_id: str, owner_qname: str, enclosing_class: str | None) -> None:
        """Record calls and local variable types, without entering nested definitions."""
        queue = list(body.children)
        while queue:
            current = queue.pop(0)
            if current.type in _DEFINITION_TYPES or current.type == "decorated_definition":
                continue
            if current.type == "call":
                self._record_call(current, owner_id, owner_qname, enclosing_class)
            elif current.type == "assignment":
                self._record_assignment(current, owner_qname)
            queue.extend(current.children)

    def _record_call(
        self, node, owner_id: str, owner_qname: str, enclosing_class: str | None
    ) -> None:
        function_node = node.child_by_field_name("function")
        if function_node is None:
            return
        if function_node.type == "identifier":
            name, receiver = self._text(function_node), None
        elif function_node.type == "attribute":
            attribute = function_node.child_by_field_name("attribute")
            object_node = function_node.child_by_field_name("object")
            if attribute is None:
                return
            name = self._text(attribute)
            receiver = self._text(object_node) if object_node is not None else None
        else:
            return

        self.result.calls.append(
            PendingCall(
                source_id=owner_id,
                module_qname=self.module_qname,
                name=name,
                receiver=receiver,
                enclosing_class=enclosing_class,
                scope_key=owner_qname,
                line=node.start_point[0] + 1,
            )
        )

    def _record_assignment(self, node, owner_qname: str) -> None:
        """Track ``x = SomeClass()`` so ``x.method()`` can be resolved later."""
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None or left.type != "identifier":
            return
        if right.type != "call":
            return
        callee = right.child_by_field_name("function")
        if callee is None or callee.type != "identifier":
            return
        self.result.scope.local_var_types.setdefault(owner_qname, {})[self._text(left)] = self._text(
            callee
        )
