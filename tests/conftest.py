from __future__ import annotations

from pathlib import Path

import pytest

from agent.provider import LLMProvider, LLMResponse, StreamComplete, TextDelta
from graph.schema import EdgeType, NodeType
from ingestion.pipeline import parse_repository
from ingestion.repo import local_repo
from ingestion.resolver import Resolver


class FakeProvider(LLMProvider):
    """Scripted provider so agent logic is testable without an API.

    ``cypher_payloads`` feeds ``generate_json`` (the single-shot path) and
    ``turns`` feeds ``converse`` (the multi-hop path); a test supplies whichever
    it exercises.
    """

    def __init__(
        self,
        cypher_payloads: list[dict] | None = None,
        answer: str = "The answer.",
        turns: list[LLMResponse] | None = None,
    ) -> None:
        self.cypher_payloads = list(cypher_payloads or [])
        self.answer = answer
        self.turns = list(turns or [])
        self.prompts: list[str] = []
        self.conversations: list[list] = []

    def generate(self, prompt, *, system=None, max_tokens=1024, effort="medium"):
        self.prompts.append(prompt)
        return LLMResponse(text=self.answer, model="fake", input_tokens=10, output_tokens=5)

    def generate_json(self, prompt, json_schema, *, system=None, max_tokens=1024, effort="medium"):
        self.prompts.append(prompt)
        payload = self.cypher_payloads.pop(0)
        return payload, LLMResponse(text="{}", model="fake", input_tokens=20, output_tokens=8)

    def converse(self, messages, tools, *, max_tokens=2048, effort="medium"):
        self.conversations.append(list(messages))
        if self.turns:
            return self.turns.pop(0)
        return LLMResponse(text=self.answer, model="fake", input_tokens=30, output_tokens=10)

    def converse_stream(self, messages, tools, *, max_tokens=2048, effort="medium"):
        """Wraps `converse` as a single-chunk stream — enough for tests that
        don't care about incremental delivery. `ChunkedProvider` below scripts
        genuine multi-chunk streams where that matters."""
        response = self.converse(messages, tools, max_tokens=max_tokens, effort=effort)
        if response.text:
            yield TextDelta(response.text)
        yield StreamComplete(response)


class ChunkedProvider(FakeProvider):
    """A FakeProvider whose final text-only turn streams in scripted pieces.

    `chunks` are the deltas for the *last* turn in `turns` (the one with no
    tool_calls); every earlier turn streams as a single chunk, same as the
    base class.
    """

    def __init__(self, *args, chunks: list[str], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.chunks = list(chunks)
        self.delta_calls: list[str] = []

    def converse_stream(self, messages, tools, *, max_tokens=2048, effort="medium"):
        self.conversations.append(list(messages))
        response = self.turns.pop(0) if self.turns else LLMResponse(
            text=self.answer, model="fake", input_tokens=30, output_tokens=10
        )
        if not response.tool_calls and self.chunks:
            for piece in self.chunks:
                self.delta_calls.append(piece)
                yield TextDelta(piece)
            yield StreamComplete(response)
            return
        if response.text:
            yield TextDelta(response.text)
        yield StreamComplete(response)


class FakeNeo4j:
    """Accepts EXPLAIN, returns fixed rows for the real query."""

    def __init__(self, rows=None, fail_on: str | None = None):
        self.rows = rows if rows is not None else [{"name": "helper"}]
        self.fail_on = fail_on
        self.executed: list[str] = []

    def run(self, query, **params):
        if self.fail_on and self.fail_on in query:
            raise RuntimeError("SyntaxError: bad query")
        self.executed.append(query)
        return [] if query.startswith("EXPLAIN") else self.rows

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
