from __future__ import annotations

from pathlib import Path

import pytest

from graph.schema import EdgeType, NodeType
from ingestion.pipeline import parse_repository
from ingestion.repo import local_repo
from ingestion.resolver import Resolver

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "sample_repo"


class Graph:
    """Parsed fixture repo, addressable by qualified name instead of node id."""

    def __init__(self, modules, resolution):
        self.modules = modules
        self.resolution = resolution
        self.nodes = {node.id: node for module in modules for node in module.fragment.nodes}
        self.edges = [
            edge for module in modules for edge in module.fragment.edges
        ] + resolution.edges
        # Mirror what the pipeline loads: resolver updates land on the nodes.
        for node_id, updates in resolution.node_updates.items():
            self.nodes[node_id].properties.update(updates)

    def qname(self, node_id: str) -> str:
        return self.nodes[node_id].properties["qualified_name"]

    def triples(self) -> set[tuple[str, str, str]]:
        return {
            (self.qname(edge.source_id), edge.type.value, self.qname(edge.target_id))
            for edge in self.edges
        }

    def node(self, qualified_name: str, node_type: NodeType):
        for node in self.nodes.values():
            if node.properties["qualified_name"] == qualified_name and node.type is node_type:
                return node
        raise KeyError(f"{node_type.value} {qualified_name!r} not found")

    def has(self, source: str, edge_type: EdgeType, target: str) -> bool:
        return (source, edge_type.value, target) in self.triples()


@pytest.fixture(scope="session")
def fixture_repo() -> Path:
    return FIXTURE_REPO


@pytest.fixture(scope="session")
def sample_graph() -> Graph:
    repo = local_repo(FIXTURE_REPO, repo_id="fixtures/sample_repo")
    modules, failures = parse_repository(repo)
    assert failures == 0
    return Graph(modules, Resolver(modules).resolve())
