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

    def generate(self, prompt, *, system=None, max_tokens=1024):
        self.prompts.append(prompt)
        return LLMResponse(text=self.answer, model="fake", input_tokens=10, output_tokens=5)

    def generate_json(self, prompt, json_schema, *, system=None, max_tokens=1024):
        self.prompts.append(prompt)
        payload = self.cypher_payloads.pop(0)
        return payload, LLMResponse(text="{}", model="fake", input_tokens=20, output_tokens=8)

    def converse(self, messages, tools, *, max_tokens=2048):
        self.conversations.append(list(messages))
        if self.turns:
            return self.turns.pop(0)
        return LLMResponse(text=self.answer, model="fake", input_tokens=30, output_tokens=10)

    def converse_stream(self, messages, tools, *, max_tokens=2048):
        """Wraps `converse` as a single-chunk stream — enough for tests that
        don't care about incremental delivery. `ChunkedProvider` below scripts
        genuine multi-chunk streams where that matters."""
        response = self.converse(messages, tools, max_tokens=max_tokens)
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

    def converse_stream(self, messages, tools, *, max_tokens=2048):
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


# ------------------------------------------------------------ web app fixtures


class FakeGraphServer:
    """Stands in for Neo4j behind the API: tracks what was deleted."""

    def __init__(self, repos: dict[str, int] | None = None) -> None:
        self.repos = dict(repos or {})
        self.deleted: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def run(self, query, **params):
        return [{"repo_id": repo_id, "nodes": nodes} for repo_id, nodes in self.repos.items()]

    def delete_repo(self, repo_id):
        self.deleted.append(repo_id)
        return self.repos.pop(repo_id, 0)


def fake_github_api(sizes: dict[str, int] | None = None, missing=(), canonical=None):
    """A GitHub REST API double: every repo exists, at ``sizes[repo]`` KB (default 100)."""
    import httpx

    sizes = sizes or {}
    canonical = canonical or {}

    def handler(request):
        parts = request.url.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "repos":
            repo_id = f"{parts[1]}/{parts[2]}"
            if repo_id in missing:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(
                200,
                json={
                    "full_name": canonical.get(repo_id, repo_id),
                    "size": sizes.get(repo_id, 100),
                    "private": False,
                    "default_branch": "main",
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class NoVectors:
    def drop(self, repo_id):
        return None


class WebApp:
    """A TestClient over a fresh app with its own database and clone root."""

    def __init__(self, client, app, graph: FakeGraphServer) -> None:
        self.client = client
        self.app = app
        self.graph = graph

    @property
    def store(self):
        return self.app.state.store

    def login_as(self, login: str, github_id: int | None = None):
        github_id = github_id if github_id is not None else abs(hash(login)) % 10_000_000 + 1
        user = self.store.upsert_user(github_id, login)
        token = self.store.create_session(user.github_id, ttl_days=1)
        self.client.cookies.set("cg_session", token)
        return user

    def logout_locally(self) -> None:
        self.client.cookies.clear()


@pytest.fixture
def web(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from api import maintenance, server
    from api.settings import Settings

    monkeypatch.setenv("REPOS_DIR", str(tmp_path / "repos"))
    graph = FakeGraphServer()
    monkeypatch.setattr(server, "_client", lambda: graph)
    monkeypatch.setattr(maintenance, "VectorStore", NoVectors)

    settings = Settings(
        app_db_path=tmp_path / "app.db",
        github_client_id="client-id",
        github_client_secret="client-secret",
        admin_logins=frozenset({"admin"}),
        min_free_disk_gb=0,
    )
    app = server.create_app(settings)
    app.state.github_transport = fake_github_api()
    with TestClient(app) as client:
        yield WebApp(client, app, graph)
