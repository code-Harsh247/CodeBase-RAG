"""Access control on the web API: who can see, ask about, add and remove what."""

from __future__ import annotations

import json
import time

import pytest
from conftest import FakeProvider

from api import server

PROTECTED = [
    ("get", "/api/repos", None),
    ("post", "/api/query", {"repo_id": "a/b", "question": "hi"}),
    ("post", "/api/ingest", {"url": "https://github.com/a/b"}),
    ("post", "/api/summarize", {"turns": [{"role": "user", "content": "hi"}]}),
    ("delete", "/api/repos/a/b", None),
    ("get", "/api/auth/me", None),
]


def _events(response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def _ready(web, repo_id: str, *owners, public: bool = False, nodes: int = 10) -> None:
    web.store.mark_ready(repo_id, nodes=nodes, files=3, public=public)
    web.graph.repos[repo_id] = nodes
    for user in owners:
        web.store.add_owner(user.github_id, repo_id)


# ------------------------------------------------------------ authentication


@pytest.mark.parametrize(("method", "path", "body"), PROTECTED)
def test_everything_but_health_needs_a_login(web, method, path, body):
    kwargs = {"json": body} if body is not None else {}
    response = getattr(web.client, method)(path, **kwargs)
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "login_required"


def test_health_is_open(web):
    assert web.client.get("/api/health").status_code == 200


def test_a_forged_cookie_is_nobody(web):
    web.client.cookies.set("cg_session", "forged")
    assert web.client.get("/api/repos").status_code == 401


def test_cross_origin_writes_are_refused(web):
    web.login_as("alice")
    response = web.client.post(
        "/api/ingest",
        json={"url": "https://github.com/a/b"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "bad_origin"


def test_same_origin_writes_are_allowed(web):
    web.login_as("alice")
    response = web.client.post(
        "/api/auth/logout", headers={"Origin": "http://localhost:5173"}
    )
    assert response.status_code == 200


# ---------------------------------------------------------------- visibility


def test_users_see_their_own_repos_and_public_ones(web):
    alice = web.login_as("alice")
    bob = web.store.upsert_user(2, "bob")
    _ready(web, "alice/lib", alice)
    _ready(web, "bob/lib", bob)
    _ready(web, "demo/repo", public=True)

    repos = {r["repo_id"]: r for r in web.client.get("/api/repos").json()["repos"]}

    assert set(repos) == {"alice/lib", "demo/repo"}
    assert repos["alice/lib"]["owned"] and not repos["alice/lib"]["public"]
    assert repos["demo/repo"]["public"] and not repos["demo/repo"]["owned"]


def test_querying_someone_elses_repo_is_a_404(web):
    web.login_as("alice")
    bob = web.store.upsert_user(2, "bob")
    _ready(web, "bob/lib", bob)

    response = web.client.post("/api/query", json={"repo_id": "bob/lib", "question": "hi"})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "repo_not_found"


def test_query_rejects_a_traversal_repo_id(web):
    web.login_as("alice")
    response = web.client.post("/api/query", json={"repo_id": "../../etc", "question": "hi"})
    assert response.status_code == 422


def test_query_uses_the_canonical_repo_id(web, monkeypatch):
    alice = web.login_as("alice")
    _ready(web, "PSF/Requests", alice)
    seen = {}

    def fake_stream(body, repo_id, provider, cancel, deadline):
        seen["repo_id"] = repo_id
        yield server._event({"type": "answer", "answer": "ok", "locations": [], "usage": {}})

    monkeypatch.setattr(server, "_stream_answer", fake_stream)
    monkeypatch.setattr(server, "get_provider", FakeProvider)
    response = web.client.post("/api/query", json={"repo_id": "psf/requests", "question": "hi"})

    assert response.status_code == 200
    assert seen["repo_id"] == "PSF/Requests"


# ----------------------------------------------------------------- ingestion


def test_ingesting_an_indexed_repo_just_adds_ownership(web, monkeypatch):
    web.store.upsert_user(2, "bob")
    _ready(web, "psf/requests", web.store.get_user(2), nodes=500)
    alice = web.login_as("alice")
    monkeypatch.setattr(server, "ingest", lambda *a, **k: pytest.fail("must not re-ingest"))

    response = web.client.post("/api/ingest", json={"url": "https://github.com/PSF/requests"})

    events = _events(response)
    assert events[-1]["type"] == "done"
    assert events[-1]["cached"] is True
    assert events[-1]["nodes"] == 500
    assert web.store.is_owner(alice.github_id, "psf/requests")


def test_a_fresh_ingest_records_ready_and_owner(web, monkeypatch):
    from ingestion.pipeline import IngestionSummary

    alice = web.login_as("alice")
    seen = {}

    def fake_ingest(source, client, **kwargs):
        seen.update(kwargs, source=source)
        return IngestionSummary(
            repo_id="new/repo", commit="abc123", files_parsed=4, files_failed=0,
            node_counts={"Function": 30}, edge_counts={"CALLS": 12}, resolution={},
            embedded=30, duration_seconds=1.0,
        )

    monkeypatch.setattr(server, "ingest", fake_ingest)
    events = _events(web.client.post("/api/ingest", json={"url": "https://github.com/new/repo"}))

    assert events[-1]["type"] == "done"
    record = web.store.get_repo("new/repo")
    assert record.status == "ready" and record.nodes == 30 and record.commit_sha == "abc123"
    assert web.store.is_owner(alice.github_id, "new/repo")
    assert seen["allow_local"] is False
    assert seen["source"] == "https://github.com/new/repo"


def test_a_failed_ingest_leaves_no_ownership(web, monkeypatch):
    alice = web.login_as("alice")

    def broken(*args, **kwargs):
        raise RuntimeError("password=hunter2 in /home/ubuntu")

    monkeypatch.setattr(server, "ingest", broken)
    response = web.client.post("/api/ingest", json={"url": "https://github.com/bad/repo"})

    events = _events(response)
    assert events[-1] == {"type": "error", "code": "internal", "message": server.ERROR_MESSAGES["internal"]}
    assert "hunter2" not in response.text
    # A failed first ingest leaves nothing behind: no record, no ownership.
    assert web.store.get_repo("bad/repo") is None
    assert not web.store.is_owner(alice.github_id, "bad/repo")
    assert web.graph.deleted == ["bad/repo"]


def test_ingest_limit_errors_keep_their_code(web, monkeypatch):
    from ingestion.limits import IngestLimitError

    web.login_as("alice")

    def too_big(*args, **kwargs):
        raise IngestLimitError("repo_too_large", "The repository has too many files.")

    monkeypatch.setattr(server, "ingest", too_big)
    events = _events(web.client.post("/api/ingest", json={"url": "https://github.com/big/repo"}))

    assert events[-1] == {
        "type": "error",
        "code": "repo_too_large",
        "message": "The repository has too many files.",
    }


def test_a_repo_being_indexed_cannot_be_started_twice(web):
    web.login_as("alice")
    web.store.begin_indexing("busy/repo")
    response = web.client.post("/api/ingest", json={"url": "https://github.com/busy/repo"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "repo_busy"


def test_non_github_urls_are_rejected_before_streaming(web):
    web.login_as("alice")
    response = web.client.post("/api/ingest", json={"url": "file:///etc/passwd"})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_url"


def test_refresh_is_ignored_for_regular_users(web, monkeypatch):
    alice = web.login_as("alice")
    _ready(web, "psf/requests", alice)
    monkeypatch.setattr(server, "ingest", lambda *a, **k: pytest.fail("must not re-ingest"))

    response = web.client.post(
        "/api/ingest", json={"url": "https://github.com/psf/requests", "refresh": True}
    )
    assert _events(response)[-1]["cached"] is True


# -------------------------------------------------------------------- delete


def test_removing_a_shared_repo_only_drops_your_claim(web):
    bob = web.store.upsert_user(2, "bob")
    alice = web.login_as("alice")
    _ready(web, "shared/lib", alice, bob)

    response = web.client.delete("/api/repos/shared/lib")

    assert response.json()["scope"] == "claim"
    assert web.graph.deleted == []
    assert not web.store.is_owner(alice.github_id, "shared/lib")
    assert web.store.can_access(bob.github_id, "shared/lib")


def test_the_last_owner_removing_it_deletes_the_data(web):
    alice = web.login_as("alice")
    _ready(web, "solo/lib", alice)

    response = web.client.delete("/api/repos/solo/lib")

    assert response.json()["scope"] == "repo"
    assert web.graph.deleted == ["solo/lib"]
    assert web.store.get_repo("solo/lib") is None


def test_public_repos_survive_their_last_owner(web):
    alice = web.login_as("alice")
    _ready(web, "demo/repo", alice, public=True)

    assert web.client.delete("/api/repos/demo/repo").json()["scope"] == "claim"
    assert web.graph.deleted == []
    assert web.store.get_repo("demo/repo").public


def test_you_cannot_delete_what_you_do_not_own(web):
    bob = web.store.upsert_user(2, "bob")
    web.login_as("alice")
    _ready(web, "bob/lib", bob)
    _ready(web, "demo/repo", public=True)

    assert web.client.delete("/api/repos/bob/lib").status_code == 404
    assert web.client.delete("/api/repos/demo/repo").status_code == 404
    assert web.graph.deleted == []


def test_admins_delete_outright(web):
    bob = web.store.upsert_user(2, "bob")
    web.login_as("admin")
    _ready(web, "bob/lib", bob)
    _ready(web, "demo/repo", public=True)

    assert web.client.delete("/api/repos/bob/lib").json()["scope"] == "repo"
    assert web.client.delete("/api/repos/demo/repo").json()["scope"] == "repo"
    assert web.graph.deleted == ["bob/lib", "demo/repo"]


def test_delete_rejects_malformed_ids(web):
    # The HTTP client normalises "owner/.." away before sending, so call the
    # handler with the values a hand-crafted request could carry. Validation
    # runs before anything else, so no request object is needed.
    from fastapi import HTTPException

    admin = server.CurrentUser(github_id=1, login="admin", avatar_url="", is_admin=True)
    for owner, name in [("owner", ".."), ("..", "etc"), ("owner", "."), ("-x", "y")]:
        with pytest.raises(HTTPException) as caught:
            server.delete_repo(owner, name, None, admin)
        assert caught.value.status_code == 400
    assert web.graph.deleted == []


# ------------------------------------------------------------------- startup


def test_startup_adopts_graph_repos_and_applies_public(tmp_path, monkeypatch):
    from conftest import FakeGraphServer
    from fastapi.testclient import TestClient

    from api.settings import Settings

    graph = FakeGraphServer({"legacy/one": 5, "legacy/two": 7})
    monkeypatch.setattr(server, "_client", lambda: graph)
    app = server.create_app(
        Settings(app_db_path=tmp_path / "a.db", public_repos=("legacy/two",))
    )
    with TestClient(app):
        store = app.state.store
        assert store.get_repo("legacy/one").status == "ready"
        assert not store.get_repo("legacy/one").public
        assert store.get_repo("legacy/two").public


# -------------------------------------------------------------------- budget


class _NoTools:
    def __init__(self, *args, **kwargs):
        pass


def _tight_budget(web, per_user: float, daily: float = 1.0):
    from agent.metering import Budget

    web.app.state.budget = Budget(web.store, daily_usd=daily, per_user_usd=per_user)


def test_questions_are_refused_up_front_when_the_budget_is_gone(web, monkeypatch):
    alice = web.login_as("alice")
    _ready(web, "a/lib", alice)
    _tight_budget(web, per_user=0.001)
    monkeypatch.setattr(server, "get_provider", lambda: pytest.fail("must not call the LLM"))

    response = web.client.post("/api/query", json={"repo_id": "a/lib", "question": "hi"})

    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "user_budget_exhausted"
    assert 0 < int(response.headers["retry-after"]) <= 86_400


def test_the_global_cap_is_reported_as_such(web):
    alice = web.login_as("alice")
    _ready(web, "a/lib", alice)
    _tight_budget(web, per_user=1.0, daily=0.001)

    response = web.client.post("/api/query", json={"repo_id": "a/lib", "question": "hi"})
    assert response.json()["detail"]["code"] == "budget_exhausted"


def test_summaries_are_metered_too(web, monkeypatch):
    web.login_as("alice")
    _tight_budget(web, per_user=0.001)
    monkeypatch.setattr(server, "get_provider", lambda: pytest.fail("must not call the LLM"))

    response = web.client.post(
        "/api/summarize", json={"turns": [{"role": "user", "content": "hi"}]}
    )
    assert response.status_code == 429


def test_running_out_mid_question_ends_the_stream_cleanly(web, monkeypatch):
    from conftest import FakeProvider

    alice = web.login_as("alice")
    _ready(web, "a/lib", alice)
    # Enough to start, not enough for the first agent turn's worst case.
    _tight_budget(web, per_user=0.002)
    monkeypatch.setattr(server, "MIN_QUERY_USD", 0.0)
    monkeypatch.setattr(server, "RetrievalTools", _NoTools)
    inner = FakeProvider()
    monkeypatch.setattr(server, "get_provider", lambda: inner)

    response = web.client.post("/api/query", json={"repo_id": "a/lib", "question": "hi"})

    events = _events(response)
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "user_budget_exhausted"
    assert inner.conversations == []  # refused before calling out


def test_answered_questions_are_charged(web, monkeypatch):
    from conftest import FakeProvider

    from agent.metering import utc_day

    class Costing(FakeProvider):
        def converse(self, messages, tools, *, max_tokens=2048):
            from agent.provider import LLMResponse

            return LLMResponse(text="It is.", model="fake", input_tokens=5, output_tokens=2,
                               cost_usd=0.0009)

    alice = web.login_as("alice")
    _ready(web, "a/lib", alice)
    monkeypatch.setattr(server, "RetrievalTools", _NoTools)
    monkeypatch.setattr(server, "get_provider", Costing)

    events = _events(web.client.post("/api/query", json={"repo_id": "a/lib", "question": "hi"}))

    assert events[-1]["type"] == "answer"
    today = utc_day(time.time())
    assert web.store.user_spend(today, alice.github_id) == pytest.approx(0.0009)


def test_closing_the_stream_cancels_the_question():
    import queue
    import threading

    events: queue.Queue = queue.Queue()
    cancel = threading.Event()
    events.put({"type": "hop"})
    stream = server._drain(events, cancel)
    next(stream)
    stream.close()  # what the server does when the client disconnects
    assert cancel.is_set()


def test_a_stuck_worker_times_out_the_stream():
    import queue
    import time as clock

    stream = server._drain(queue.Queue(), deadline=clock.monotonic() - 1)
    frames = list(stream)
    assert '"code": "timeout"' in frames[-1]
