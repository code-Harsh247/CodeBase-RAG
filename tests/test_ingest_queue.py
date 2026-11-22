"""The ingest line, repository locks, eviction, and how the API uses them."""

from __future__ import annotations

import json
import threading
import time

import pytest

from api import server
from api.ingest_queue import IngestCoordinator
from api.maintenance import evict
from api.store import AppStore
from ingestion.limits import IngestLimitError
from ingestion.pipeline import IngestionSummary
from tests.conftest import FakeGraphServer, fake_github_api

# ---------------------------------------------------------------- the line


def test_line_has_a_fixed_capacity():
    line = IngestCoordinator(workers=1, max_waiting=2)
    tickets = [line.try_enqueue() for _ in range(3)]
    assert all(tickets)
    assert line.try_enqueue() is None
    tickets[0].release()
    assert line.try_enqueue() is not None


def test_tickets_run_one_at_a_time_in_order():
    line = IngestCoordinator(workers=1, max_waiting=5)
    tickets = [line.try_enqueue() for _ in range(3)]
    order: list[int] = []

    def run(index):
        tickets[index].wait(poll_s=0.01)
        order.append(index)
        time.sleep(0.02)
        tickets[index].release()

    threads = [threading.Thread(target=run, args=(i,)) for i in (2, 1, 0)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert order == [0, 1, 2]


def test_waiting_tickets_hear_their_position():
    line = IngestCoordinator(workers=1, max_waiting=5)
    first, second = line.try_enqueue(), line.try_enqueue()
    first.wait()
    positions: list[int] = []
    waiter = threading.Thread(target=second.wait, kwargs={"on_position": positions.append, "poll_s": 0.01})
    waiter.start()
    time.sleep(0.05)
    first.release()
    waiter.join(2)
    assert positions == [1]
    second.release()


def test_a_cancelled_wait_gives_up_its_place():
    line = IngestCoordinator(workers=1, max_waiting=1)
    running, waiting = line.try_enqueue(), line.try_enqueue()
    running.wait()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(IngestLimitError) as caught:
        waiting.wait(cancel=cancel, poll_s=0.01)
    assert caught.value.code == "cancelled"
    assert line.outstanding == 1
    waiting.release()  # releasing twice is harmless
    assert line.outstanding == 1


def test_repo_locks_are_case_insensitive_and_time_out():
    line = IngestCoordinator()
    with (
        line.repo_lock("PSF/Requests"),
        pytest.raises(TimeoutError),
        line.repo_lock("psf/requests", timeout_s=0.05),
    ):
        pass
    with line.repo_lock("psf/requests", timeout_s=0.05):
        pass


# ------------------------------------------------------------------ eviction


@pytest.fixture
def store(tmp_path):
    db = AppStore(tmp_path / "app.db")
    yield db
    db.close()


@pytest.fixture
def no_side_effects(monkeypatch, tmp_path):
    from api import maintenance
    from tests.conftest import _NoVectors

    monkeypatch.setenv("REPOS_DIR", str(tmp_path / "repos"))
    monkeypatch.setattr(maintenance, "VectorStore", _NoVectors)


def _seed(store, repo_id, *, used, owners=(1,), public=False, status="ready"):
    store.upsert_user(1, "alice")
    store.mark_ready(repo_id, public=public)
    store._db.execute("UPDATE repos SET last_used_at = ? WHERE repo_id = ?", (used, repo_id))
    for owner in owners:
        store.add_owner(owner, repo_id)
    if status == "failed":
        store.mark_failed(repo_id)


def test_failed_ingests_are_always_cleaned_up(store, no_side_effects):
    _seed(store, "a/failed", used=0, status="failed")
    _seed(store, "a/fine", used=0)
    gone = evict(store, FakeGraphServer(), IngestCoordinator(), max_indexed=None, idle_ttl_s=None)
    assert gone == ["a/failed"]


def test_eviction_off_never_touches_working_repos(store, no_side_effects):
    _seed(store, "a/orphan", used=0, owners=())
    _seed(store, "a/old", used=0)
    gone = evict(store, FakeGraphServer(), IngestCoordinator(), max_indexed=None, idle_ttl_s=None)
    assert gone == []


def test_orphans_and_idle_repos_go_when_enabled(store, no_side_effects):
    now = 1_000_000.0
    _seed(store, "a/orphan", used=now, owners=())
    _seed(store, "a/idle", used=now - 10 * 86_400)
    _seed(store, "a/fresh", used=now)
    _seed(store, "demo/public", used=0, owners=(), public=True)

    gone = evict(
        store, FakeGraphServer(), IngestCoordinator(),
        max_indexed=100, idle_ttl_s=7 * 86_400, now=now,
    )
    assert sorted(gone) == ["a/idle", "a/orphan"]
    assert store.get_repo("demo/public") is not None


def test_least_recently_used_go_first_to_make_room(store, no_side_effects):
    for age, repo in enumerate(["a/newest", "a/middle", "a/oldest"]):
        _seed(store, repo, used=1000 - age)
    _seed(store, "demo/public", used=0, public=True)

    gone = evict(store, FakeGraphServer(), IngestCoordinator(), max_indexed=3, idle_ttl_s=None)

    # 4 repos, room for 2 plus the incoming one: the oldest private two go.
    assert gone == ["a/oldest", "a/middle"]


def test_eviction_spares_the_incoming_and_busy_repos(store, no_side_effects):
    _seed(store, "a/incoming", used=0, owners=())
    _seed(store, "a/locked", used=0, owners=())
    line = IngestCoordinator()
    with line.repo_lock("a/locked"):
        gone = evict(store, FakeGraphServer(), line, max_indexed=10, idle_ttl_s=None, keep="a/incoming")
    assert gone == []


# --------------------------------------------------------------------- API


def _events(response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def _summary(repo_id: str) -> IngestionSummary:
    return IngestionSummary(
        repo_id=repo_id, commit="abc", files_parsed=1, node_counts={"Function": 3},
        edge_counts={}, embedded=3, duration_seconds=0.1,
    )


def test_oversized_repos_are_refused_before_cloning(web, monkeypatch):
    web.login_as("alice")
    web.app.state.github_transport = fake_github_api(sizes={"big/repo": 500 * 1024})
    monkeypatch.setattr(server, "ingest", lambda *a, **k: pytest.fail("must not clone"))

    response = web.client.post("/api/ingest", json={"url": "https://github.com/big/repo"})

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "repo_too_large"
    assert web.store.get_repo("big/repo") is None


def test_missing_repos_are_a_404_before_cloning(web, monkeypatch):
    web.login_as("alice")
    web.app.state.github_transport = fake_github_api(missing={"no/such"})
    monkeypatch.setattr(server, "ingest", lambda *a, **k: pytest.fail("must not clone"))

    response = web.client.post("/api/ingest", json={"url": "https://github.com/no/such"})

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "repo_not_found"


def test_the_canonical_name_is_what_gets_indexed(web, monkeypatch):
    alice = web.login_as("alice")
    web.app.state.github_transport = fake_github_api(canonical={"PSF/Requests": "psf/requests"})
    seen = {}

    def fake_ingest(source, client, **kwargs):
        seen["source"] = source
        return _summary("psf/requests")

    monkeypatch.setattr(server, "ingest", fake_ingest)
    web.client.post("/api/ingest", json={"url": "https://github.com/PSF/Requests"})

    assert seen["source"] == "https://github.com/psf/requests"
    assert web.store.get_repo("psf/requests").repo_id == "psf/requests"
    assert web.store.is_owner(alice.github_id, "psf/requests")


def test_ingest_gets_bounded_limits(web, monkeypatch):
    web.login_as("alice")
    seen = {}

    def fake_ingest(source, client, **kwargs):
        seen.update(kwargs)
        return _summary("a/b")

    monkeypatch.setattr(server, "ingest", fake_ingest)
    web.client.post("/api/ingest", json={"url": "https://github.com/a/b"})

    limits = seen["limits"]
    assert limits.max_files and limits.max_total_bytes and limits.max_graph_nodes
    assert limits.clone_timeout_s and limits.max_clone_disk_bytes and limits.deadline


def test_per_user_project_limit(web, monkeypatch):
    alice = web.login_as("alice")
    for i in range(web.app.state.settings.max_repos_per_user):
        web.store.mark_ready(f"own/r{i}")
        web.store.add_owner(alice.github_id, f"own/r{i}")
    web.store.mark_ready("someone/else")
    monkeypatch.setattr(server, "ingest", lambda *a, **k: pytest.fail("must not clone"))

    for url in ("https://github.com/new/repo", "https://github.com/someone/else"):
        response = web.client.post("/api/ingest", json={"url": url})
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "repo_limit"

    # Re-adding one you already have is not a new project.
    response = web.client.post("/api/ingest", json={"url": "https://github.com/own/r0"})
    assert response.status_code == 200


def test_a_full_line_is_a_503_before_streaming(web):
    web.login_as("alice")
    line = web.app.state.coordinator
    held = [line.try_enqueue() for _ in range(line.workers + line.max_waiting)]

    response = web.client.post("/api/ingest", json={"url": "https://github.com/a/b"})

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ingest_busy"
    assert response.headers["retry-after"]
    assert web.store.get_repo("a/b") is None
    for ticket in held:
        ticket.release()


def test_queued_ingests_report_their_place_then_run(web, monkeypatch):
    web.login_as("alice")
    line = web.app.state.coordinator
    blocker = line.try_enqueue()
    blocker.wait()
    monkeypatch.setattr(server, "ingest", lambda *a, **k: _summary("a/b"))
    threading.Timer(0.3, blocker.release).start()

    events = _events(web.client.post("/api/ingest", json={"url": "https://github.com/a/b"}))

    stages = [e.get("stage") for e in events if e["type"] == "progress"]
    assert "queued" in stages
    assert events[-1]["type"] == "done"
    assert line.outstanding == 0


def test_a_failed_ingest_frees_the_line(web, monkeypatch):
    web.login_as("alice")

    def too_slow(*args, **kwargs):
        raise IngestLimitError("timeout", "Indexing took too long and was stopped.")

    monkeypatch.setattr(server, "ingest", too_slow)
    events = _events(web.client.post("/api/ingest", json={"url": "https://github.com/a/b"}))

    assert events[-1]["code"] == "timeout"
    assert web.app.state.coordinator.outstanding == 0
    assert web.store.get_repo("a/b") is None


def test_removing_a_repo_that_is_being_indexed_waits_its_turn(web, monkeypatch):
    monkeypatch.setattr(server, "REPO_LOCK_WAIT_S", 0.05)
    alice = web.login_as("alice")
    web.store.mark_ready("a/b")
    web.store.add_owner(alice.github_id, "a/b")
    with web.app.state.coordinator.repo_lock("a/b"):
        response = web.client.delete("/api/repos/a/b")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "repo_busy"
    assert web.store.is_owner(alice.github_id, "a/b")


def test_server_full_stops_ingest(web, monkeypatch):
    web.login_as("alice")
    monkeypatch.setattr(server.shutil, "disk_usage", lambda path: type("U", (), {"free": 0})())
    monkeypatch.setattr(server, "ingest", lambda *a, **k: pytest.fail("must not clone"))
    web.app.state.settings.__dict__["min_free_disk_gb"] = 5  # frozen dataclass; test only

    events = _events(web.client.post("/api/ingest", json={"url": "https://github.com/a/b"}))

    assert events[-1]["code"] == "server_full"
