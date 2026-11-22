"""Operations shared by the server and the admin CLI.

Kept out of ``api/server.py`` so the CLI can delete or reconcile without
constructing the web app.
"""

from __future__ import annotations

import logging
import time

from api.store import AppStore
from graph.schema import SHARED_LABEL
from ingestion.repo import remove_tree, repo_dir
from retrieval.vector_store import VectorStore

logger = logging.getLogger(__name__)


def physical_delete(client, repo_id: str, store: AppStore | None = None) -> dict:
    """Remove a repository everywhere: graph, embeddings, clone and record.

    Destructive and irreversible; re-adding it means indexing again. Callers
    decide *whether* this is allowed — see the ownership rules in the server.
    """
    target = repo_dir(repo_id)  # validates; raises ValueError on a bad id
    removed = client.delete_repo(repo_id)

    try:
        VectorStore().drop(repo_id)
    except Exception as exc:  # noqa: BLE001 - the graph is already gone
        logger.warning("could not drop embeddings for %s: %s", repo_id, exc)

    source_removed = False
    if target.is_dir():
        remove_tree(target)
        source_removed = not target.exists()

    if store is not None:
        store.forget_repo(repo_id)

    logger.info("deleted %s: %d nodes, source=%s", repo_id, removed, source_removed)
    return {"repo_id": repo_id, "nodes_removed": removed, "source_removed": source_removed}


def graph_repo_counts(client) -> dict[str, int]:
    """Every repo id present in the graph, with its node count."""
    rows = client.run(
        f"MATCH (n:{SHARED_LABEL}) WHERE n.repo_id IS NOT NULL "
        f"RETURN n.repo_id AS repo_id, count(n) AS nodes"
    )
    return {row["repo_id"]: row["nodes"] for row in rows}


def reconcile(store: AppStore, client, *, fail_indexing: bool = True) -> dict[str, int]:
    return store.reconcile(graph_repo_counts(client), fail_indexing=fail_indexing)


def evict(
    store: AppStore,
    client,
    coordinator,
    *,
    max_indexed: int | None,
    idle_ttl_s: float | None,
    keep: str | None = None,
    now: float | None = None,
) -> list[str]:
    """Delete repositories to make room for a new one. Returns what was deleted.

    Public repositories, ones being indexed, and ``keep`` are never touched.
    Always removes failed ingests (their data is incomplete anyway). When
    ``max_indexed`` / ``idle_ttl_s`` are set, also removes repositories nobody
    owns, ones unused for ``idle_ttl_s``, and then the least recently used
    until fewer than ``max_indexed`` remain, leaving room for one more.

    Each deletion takes the repository lock without waiting and re-checks the
    reason under it, so a repo someone just added or is indexing is skipped.
    """
    now = time.time() if now is None else now
    keep_key = keep.lower() if keep else None
    evicted: list[str] = []

    def is_candidate(record) -> bool:
        return (
            record is not None
            and not record.public
            and record.status != "indexing"
            and record.repo_id.lower() != keep_key
        )

    def drop(repo_id: str, reason) -> None:
        try:
            with coordinator.repo_lock(repo_id, timeout_s=0):
                current = store.get_repo(repo_id)
                if not is_candidate(current) or not reason(current):
                    return
                physical_delete(client, current.repo_id, store)
                evicted.append(current.repo_id)
                logger.info("evicted %s", current.repo_id)
        except TimeoutError:
            return  # busy right now; try again next time
        except Exception:  # one bad repo must not block ingestion
            logger.warning("could not evict %s", repo_id, exc_info=True)

    def stale(record) -> bool:
        if record.status == "failed":
            return True
        if max_indexed is None and idle_ttl_s is None:
            return False
        orphaned = record.owner_count == 0
        idle = idle_ttl_s is not None and record.last_used_at < now - idle_ttl_s
        return orphaned or idle

    for record in store.list_repos():
        if is_candidate(record) and stale(record):
            drop(record.repo_id, stale)

    if max_indexed is not None:
        records = store.list_repos()
        by_age = sorted((r for r in records if is_candidate(r)), key=lambda r: r.last_used_at)
        excess = len(records) - (max_indexed - 1)
        for record in by_age[: max(excess, 0)]:
            drop(record.repo_id, lambda _current: True)

    return evicted
