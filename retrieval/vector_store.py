"""Semantic index over code summaries.

What gets embedded is a natural-language *summary* — qualified name, signature
and docstring — not raw source. Questions are asked in English, and English
descriptions of intent match them far better than the syntax of an
implementation does.

Every record is keyed by its graph node id, which is what lets a semantic hit be
expanded into its graph neighbourhood instead of being answered in isolation.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import chromadb
from chromadb.config import Settings

from graph.schema import Node, NodeType

logger = logging.getLogger(__name__)

DEFAULT_PERSIST_DIR = Path(".chroma")

#: Only definitions carry meaning worth searching semantically. Files, Modules
#: and Imports are better reached through the graph.
INDEXED_TYPES = (NodeType.CLASS, NodeType.FUNCTION, NodeType.METHOD)

#: A docstring beyond this adds noise rather than signal to the embedding.
MAX_DOCSTRING_CHARS = 600


@dataclass
class SemanticHit:
    node_id: str
    qualified_name: str
    node_type: str
    file_path: str
    start_line: int | None
    summary: str
    distance: float


def build_summary(node: Node) -> str:
    """The text embedded for one definition."""
    properties = node.properties
    parts = [f"{node.type.value} {properties.get('qualified_name', '')}"]

    signature = properties.get("signature")
    if signature:
        parts.append(signature)

    docstring = properties.get("docstring")
    if docstring:
        parts.append(str(docstring)[:MAX_DOCSTRING_CHARS])

    return "\n".join(parts)


class VectorStore:
    """Chroma-backed semantic index, one collection per repository."""

    def __init__(self, persist_dir: Path | str | None = None) -> None:
        directory = Path(
            persist_dir or os.environ.get("CHROMA_PERSIST_DIR") or DEFAULT_PERSIST_DIR
        )
        directory.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=str(directory), settings=Settings(anonymized_telemetry=False)
        )

    @staticmethod
    def _collection_name(repo_id: str) -> str:
        """A hash of the lower-cased id: one name per repository, whatever its spelling.

        Chroma allows only a limited character set, and an earlier scheme that
        substituted the rest (``/`` -> ``__``, ``.`` -> ``_``) mapped
        ``u/my.lib`` and ``u/my_lib`` to the same collection.
        """
        return "repo_" + hashlib.sha1(repo_id.lower().encode()).hexdigest()[:20]

    @staticmethod
    def _legacy_collection_name(repo_id: str) -> str:
        """The pre-hash name, still read so existing indexes keep working."""
        return "repo_" + repo_id.replace("/", "__").replace(".", "_")

    def _collection(self, repo_id: str):
        return self._client.get_or_create_collection(
            name=self._collection_name(repo_id), metadata={"hnsw:space": "cosine"}
        )

    def _existing_collection(self, repo_id: str):
        """The collection holding the index for ``repo_id``, or None. Never creates one."""
        for name in (self._collection_name(repo_id), self._legacy_collection_name(repo_id)):
            try:
                collection = self._client.get_collection(name)
            except Exception:  # noqa: BLE001, S112 - Chroma raises various "not found" types
                continue
            if collection.count():
                return collection
        return None

    def drop(self, repo_id: str) -> None:
        for name in (self._collection_name(repo_id), self._legacy_collection_name(repo_id)):
            try:
                self._client.delete_collection(name)
            except Exception as exc:  # noqa: BLE001 - absent collection is not an error
                logger.debug("no collection %s to drop for %s: %s", name, repo_id, exc)

    def index(
        self,
        repo_id: str,
        nodes: list[Node],
        batch_size: int = 200,
        check: Callable[[], None] | None = None,
    ) -> int:
        """Replace the index for ``repo_id`` with summaries of ``nodes``.

        ``check`` runs before each batch; raising from it abandons the index
        (embedding is the slowest stage, so this is where a deadline bites).
        """
        indexable = [node for node in nodes if node.type in INDEXED_TYPES]
        self.drop(repo_id)
        if not indexable:
            return 0

        collection = self._collection(repo_id)
        for start in range(0, len(indexable), batch_size):
            if check is not None:
                check()
            batch = indexable[start : start + batch_size]
            collection.add(
                ids=[node.id for node in batch],
                documents=[build_summary(node) for node in batch],
                metadatas=[
                    {
                        "qualified_name": node.properties.get("qualified_name", ""),
                        "node_type": node.type.value,
                        "file_path": node.properties.get("file_path", ""),
                        "start_line": node.properties.get("start_line") or 0,
                    }
                    for node in batch
                ],
            )
        return len(indexable)

    def search(self, repo_id: str, query: str, limit: int = 8) -> list[SemanticHit]:
        collection = self._existing_collection(repo_id)
        if collection is None:
            return []

        found = collection.query(
            query_texts=[query], n_results=min(limit, collection.count())
        )
        hits: list[SemanticHit] = []
        for node_id, document, metadata, distance in zip(
            found["ids"][0],
            found["documents"][0],
            found["metadatas"][0],
            found["distances"][0],
            strict=True,
        ):
            hits.append(
                SemanticHit(
                    node_id=node_id,
                    qualified_name=str(metadata.get("qualified_name", "")),
                    node_type=str(metadata.get("node_type", "")),
                    file_path=str(metadata.get("file_path", "")),
                    start_line=int(metadata.get("start_line") or 0) or None,
                    summary=document,
                    distance=distance,
                )
            )
        return hits
