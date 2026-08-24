"""Semantic index over code summaries.

What gets embedded is a natural-language *summary* — qualified name, signature
and docstring — not raw source. Questions are asked in English, and English
descriptions of intent match them far better than the syntax of an
implementation does.

Every record is keyed by its graph node id, which is what lets a semantic hit be
expanded into its graph neighbourhood instead of being answered in isolation.
"""

from __future__ import annotations

import logging
import os
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
        # Chroma names allow a limited character set; repo ids contain "/".
        return "repo_" + repo_id.replace("/", "__").replace(".", "_")

    def _collection(self, repo_id: str):
        return self._client.get_or_create_collection(
            name=self._collection_name(repo_id), metadata={"hnsw:space": "cosine"}
        )

    def drop(self, repo_id: str) -> None:
        try:
            self._client.delete_collection(self._collection_name(repo_id))
        except Exception as exc:  # noqa: BLE001 - absent collection is not an error
            logger.debug("no collection to drop for %s: %s", repo_id, exc)

    def index(self, repo_id: str, nodes: list[Node], batch_size: int = 200) -> int:
        """Replace the index for ``repo_id`` with summaries of ``nodes``."""
        indexable = [node for node in nodes if node.type in INDEXED_TYPES]
        self.drop(repo_id)
        if not indexable:
            return 0

        collection = self._collection(repo_id)
        for start in range(0, len(indexable), batch_size):
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
        collection = self._collection(repo_id)
        if collection.count() == 0:
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
