from __future__ import annotations

import sqlite3
import threading

import pytest

from api.store import AppStore, hash_token


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    db = AppStore(tmp_path / "app.db", clock=clock)
    yield db
    db.close()


def test_migrations_are_idempotent(tmp_path):
    first = AppStore(tmp_path / "a.db")
    first.close()
    second = AppStore(tmp_path / "a.db")  # reopening must not re-run anything
    assert second.migrate() == 2
    second.close()


# ---------------------------------------------------------------- sessions


def test_sessions_store_only_a_hash(store, tmp_path):
    user = store.upsert_user(1, "alice")
    token = store.create_session(user.github_id, ttl_days=1)

    raw = sqlite3.connect(tmp_path / "app.db").execute("SELECT token_hash FROM sessions")
    hashes = [row[0] for row in raw]
    assert hashes == [hash_token(token)]
    assert token not in hashes
    assert store.user_for_session(token).login == "alice"


def test_sessions_expire(store, clock):
    store.upsert_user(1, "alice")
    token = store.create_session(1, ttl_days=1)
    clock.now += 86_401
    assert store.user_for_session(token) is None
    assert store.purge_expired_sessions() == 1


def test_unknown_or_empty_tokens_are_nobody(store):
    assert store.user_for_session("") is None
    assert store.user_for_session("not-a-session") is None


def test_logout_ends_only_that_session(store):
    store.upsert_user(1, "alice")
    phone = store.create_session(1, ttl_days=1)
    laptop = store.create_session(1, ttl_days=1)
    store.delete_session(phone)
    assert store.user_for_session(phone) is None
    assert store.user_for_session(laptop) is not None


def test_ban_ends_every_session(store):
    store.upsert_user(1, "alice")
    token = store.create_session(1, ttl_days=1)
    assert store.set_banned("ALICE", True).banned
    assert store.user_for_session(token) is None
    assert not store.set_banned("alice", False).banned


def test_upsert_tracks_login_renames(store):
    store.upsert_user(1, "alice")
    store.upsert_user(1, "alice-renamed", "https://avatar")
    assert store.get_user(1).login == "alice-renamed"
    assert store.get_user_by_login("Alice-Renamed").github_id == 1


# ------------------------------------------------------------------- repos


def test_repo_ids_are_case_insensitive(store):
    store.mark_ready("PSF/Requests", nodes=10)
    assert store.get_repo("psf/requests").repo_id == "PSF/Requests"


def test_visibility_is_owned_plus_public(store):
    for github_id, login in ((1, "alice"), (2, "bob")):
        store.upsert_user(github_id, login)
    store.mark_ready("alice/lib")
    store.mark_ready("bob/lib")
    store.mark_ready("demo/repo", public=True)
    store.add_owner(1, "alice/lib")
    store.add_owner(2, "bob/lib")

    seen = {r.repo_id: r.owned for r in store.visible_repos(1)}
    assert seen == {"alice/lib": True, "demo/repo": False}
    assert store.can_access(1, "alice/lib")
    assert store.can_access(1, "demo/repo")
    assert not store.can_access(1, "bob/lib")


def test_repos_still_indexing_are_not_visible(store):
    store.upsert_user(1, "alice")
    assert store.begin_indexing("alice/lib")
    store.add_owner(1, "alice/lib")
    assert store.visible_repos(1) == []
    assert not store.can_access(1, "alice/lib")


def test_only_one_indexer_at_a_time(store):
    assert store.begin_indexing("a/b")
    assert not store.begin_indexing("A/B")
    store.mark_failed("a/b")
    assert store.begin_indexing("a/b")


def test_concurrent_claims_admit_exactly_one(store):
    results: list[bool] = []
    barrier = threading.Barrier(8)

    def claim():
        barrier.wait()
        results.append(store.begin_indexing("race/repo"))

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1


def test_owner_counting(store):
    for github_id in (1, 2):
        store.upsert_user(github_id, f"u{github_id}")
    store.mark_ready("x/y")
    store.add_owner(1, "x/y")
    store.add_owner(2, "x/y")
    store.add_owner(2, "x/y")  # idempotent
    assert store.get_repo("x/y").owner_count == 2
    assert store.remove_owner(1, "x/y") == 1
    assert store.remove_owner(2, "x/y") == 0


def test_forgetting_a_repo_drops_its_claims(store):
    store.upsert_user(1, "alice")
    store.mark_ready("x/y")
    store.add_owner(1, "x/y")
    store.forget_repo("x/y")
    assert store.get_repo("x/y") is None
    assert store.owned_count(1) == 0


def test_apply_public_sets_exactly_the_listed_repos(store):
    store.mark_ready("a/one", public=True)
    store.mark_ready("a/two")
    store.apply_public(["A/TWO"])
    assert not store.get_repo("a/one").public
    assert store.get_repo("a/two").public


def test_mark_ready_keeps_public_flag_unless_told(store):
    store.mark_ready("a/one", public=True)
    store.mark_ready("a/one", nodes=5)
    assert store.get_repo("a/one").public


# --------------------------------------------------------------- reconcile


def test_reconcile_adopts_and_flags(store):
    store.mark_ready("gone/repo")
    store.begin_indexing("crashed/repo")

    counts = store.reconcile({"legacy/repo": 42})

    assert counts == {"adopted": 1, "failed": 2}
    legacy = store.get_repo("legacy/repo")
    assert legacy.status == "ready" and legacy.nodes == 42 and legacy.owner_count == 0
    assert store.get_repo("gone/repo").status == "failed"
    assert store.get_repo("crashed/repo").status == "failed"


def test_reconcile_can_leave_live_ingests_alone(store):
    store.begin_indexing("busy/repo")
    store.reconcile({}, fail_indexing=False)
    assert store.get_repo("busy/repo").status == "indexing"
