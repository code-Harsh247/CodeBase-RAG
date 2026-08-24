from __future__ import annotations

from graph.schema import EdgeType, NodeType


def test_inheritance_across_classes(sample_graph):
    assert sample_graph.has("pkg.models.Dog", EdgeType.INHERITS, "pkg.models.Animal")


def test_self_call_resolves_within_class(sample_graph):
    assert sample_graph.has(
        "pkg.models.Animal.describe", EdgeType.CALLS, "pkg.models.Animal.speak"
    )


def test_self_call_prefers_the_override_on_the_subclass(sample_graph):
    assert sample_graph.has("pkg.models.Dog.fetch", EdgeType.CALLS, "pkg.models.Dog.speak")
    assert not sample_graph.has("pkg.models.Dog.fetch", EdgeType.CALLS, "pkg.models.Animal.speak")


def test_call_through_relative_import(sample_graph):
    assert sample_graph.has("pkg.models.Dog.speak", EdgeType.CALLS, "pkg.utils.helper")


def test_call_through_absolute_import(sample_graph):
    assert sample_graph.has("app.main", EdgeType.CALLS, "pkg.services.make_dog")


def test_instantiation_is_a_reference_not_a_call(sample_graph):
    assert sample_graph.has("pkg.services.make_dog", EdgeType.REFERENCES, "pkg.models.Dog")
    assert not sample_graph.has("pkg.services.make_dog", EdgeType.CALLS, "pkg.models.Dog")


def test_local_variable_type_inference(sample_graph):
    # d = Dog(); d.speak()
    assert sample_graph.has("pkg.services.make_dog", EdgeType.CALLS, "pkg.models.Dog.speak")


def test_parameter_type_inference(sample_graph):
    # def add(self, dog: Dog) -> None: dog.speak()
    assert sample_graph.has("pkg.services.Kennel.add", EdgeType.CALLS, "pkg.models.Dog.speak")
    # def unused(animal: Animal) -> None: animal.describe()
    assert sample_graph.has("app.unused", EdgeType.CALLS, "pkg.models.Animal.describe")


def test_type_annotations_become_references(sample_graph):
    assert sample_graph.has("pkg.services.Kennel.add", EdgeType.REFERENCES, "pkg.models.Dog")
    assert sample_graph.has("app.unused", EdgeType.REFERENCES, "pkg.models.Animal")


def test_imports_guarded_by_type_checking_are_registered(sample_graph):
    # `if TYPE_CHECKING: from pkg.models import Dog` must still bind Dog.
    assert sample_graph.has("pkg.guarded.describe", EdgeType.REFERENCES, "pkg.models.Dog")
    assert sample_graph.has("pkg.guarded.describe", EdgeType.CALLS, "pkg.models.Dog.speak")


def test_return_annotations_become_references(sample_graph):
    # find_dog only mentions Dog in its return type, never instantiating it.
    assert sample_graph.has("pkg.services.find_dog", EdgeType.REFERENCES, "pkg.models.Dog")


def test_internal_imports_link_to_their_module(sample_graph):
    import_nodes = [
        node for node in sample_graph.nodes.values() if node.type is NodeType.IMPORT
    ]
    internal = {node.properties["name"] for node in import_nodes if node.properties["is_internal"]}
    external = {
        node.properties["name"] for node in import_nodes if not node.properties["is_internal"]
    }
    assert {"Dog", "make_dog", "Animal", "helper"} <= internal
    assert "os" in external


def test_reexport_through_package_init(sample_graph):
    # app.py does `from pkg import Animal`, and pkg/__init__.py re-exports it.
    assert sample_graph.has("app.unused", EdgeType.REFERENCES, "pkg.models.Animal")


def test_external_calls_are_not_invented(sample_graph):
    # os.path.join() must not resolve to anything in the repo.
    targets = {
        target
        for source, edge_type, target in sample_graph.triples()
        if source == "app.main" and edge_type == EdgeType.CALLS.value
    }
    assert targets == {"pkg.services.make_dog"}


def test_resolution_stats_are_reported(sample_graph):
    stats = sample_graph.resolution.stats
    assert stats.calls_total > 0
    assert stats.calls_resolved > 0
    assert stats.bases_resolved == 1
