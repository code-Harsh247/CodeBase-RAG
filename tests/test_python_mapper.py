from __future__ import annotations

import pytest

from graph.schema import EdgeType, NodeType, make_node_id
from ingestion.python_mapper import module_qname_for


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("app.py", "app"),
        ("pkg/models.py", "pkg.models"),
        ("pkg/__init__.py", "pkg"),
        # A src/ layout is imported as `requests.api`, not `src.requests.api`.
        ("src/requests/api.py", "requests.api"),
        ("src/requests/__init__.py", "requests"),
        # Directories that are not packages do not contribute to the name.
        ("scripts/tool.py", "tool"),
    ],
)
def test_module_qname_for(tmp_path, path, expected):
    packages = {"pkg", "src/requests"}
    for package in packages:
        directory = tmp_path / package
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "__init__.py").touch()

    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()

    assert module_qname_for(tmp_path, target) == expected


def test_node_ids_are_deterministic():
    args = ("owner/name", "pkg/models.py", "pkg.models.Dog", NodeType.CLASS)
    assert make_node_id(*args) == make_node_id(*args)


def test_node_ids_differ_by_type():
    first = make_node_id("r", "a.py", "a", NodeType.FILE)
    second = make_node_id("r", "a.py", "a", NodeType.MODULE)
    assert first != second


def test_definitions_are_extracted(sample_graph):
    for qualified_name, node_type in [
        ("pkg.models.Animal", NodeType.CLASS),
        ("pkg.models.Dog", NodeType.CLASS),
        ("pkg.models.Dog.speak", NodeType.METHOD),
        ("pkg.utils.helper", NodeType.FUNCTION),
        ("pkg.services.make_dog", NodeType.FUNCTION),
        ("app", NodeType.MODULE),
        ("app.py", NodeType.FILE),
    ]:
        assert sample_graph.node(qualified_name, node_type)


def test_methods_are_methods_not_functions(sample_graph):
    assert sample_graph.node("pkg.models.Dog.speak", NodeType.METHOD).type is NodeType.METHOD
    with pytest.raises(KeyError):
        sample_graph.node("pkg.models.Dog.speak", NodeType.FUNCTION)


def test_signature_and_line_numbers_are_captured(sample_graph):
    node = sample_graph.node("pkg.models.Dog.fetch", NodeType.METHOD)
    assert node.properties["signature"] == "(self, times: int) -> None"
    assert node.properties["start_line"] < node.properties["end_line"]
    assert node.properties["file_path"] == "pkg/models.py"


def test_containment_edges(sample_graph):
    assert sample_graph.has("app.py", EdgeType.CONTAINS, "app")
    assert sample_graph.has("pkg.models", EdgeType.CONTAINS, "pkg.models.Dog")
    assert sample_graph.has("pkg.models.Dog", EdgeType.DEFINES, "pkg.models.Dog.speak")
