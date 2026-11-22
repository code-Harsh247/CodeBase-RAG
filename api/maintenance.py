"""Operations shared by the server and the admin CLI.

Kept out of ``api/server.py`` so the CLI can delete or reconcile without
constructing the web app.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat

from api.store import AppStore
from graph.schema import SHARED_LABEL
from ingestion.repo import repo_dir
from retrieval.vector_store import VectorStore

logger = logging.getLogger(__name__)


def _force_remove(func, path, _exc) -> None:
    """Clear the read-only bit and retry.

    Git marks files under `.git/objects` read-only, which makes `rmtree` fail
    on Windows. Passing `ignore_errors` instead would leave the clone on disk
    and report success, which is worse than failing loudly.
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


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
        shutil.rmtree(target, onexc=_force_remove)
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
