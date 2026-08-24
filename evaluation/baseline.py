"""Naive chunk-and-embed RAG, as the comparison point.

This is the approach the project argues against: split every file into
fixed-size line windows, embed them, retrieve the top-k most similar to the
question, and hand those to the model. No parsing, no graph, no iteration.

It is deliberately built to be *fair*, not to be a straw man:

* Same embedding model as the graph system (Chroma's default ONNX MiniLM), so
  the comparison isolates retrieval strategy rather than embedding quality.
* Same LLM, same answer-formatting instructions.
* Chunk size and overlap are ordinary defaults, not tuned to fail.

What it cannot do is follow a relationship. That is the point of the
comparison, and it should lose on relational questions for that reason rather
than because it was handicapped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import chromadb
from chromadb.config import Settings

from agent.provider import LLMProvider, Usage
from agent.query_agent import _tidy_answer
from ingestion.walker import walk_files
from retrieval.locations import Location, dedupe

logger = logging.getLogger(__name__)

#: Ordinary defaults for code chunking — big enough to hold a small function,
#: with overlap so a definition split across a boundary still appears whole
#: somewhere.
CHUNK_LINES = 40
CHUNK_OVERLAP = 10
TOP_K = 8

ANSWER_MAX_TOKENS = 1500

_ANSWER_SYSTEM = (
    "You answer questions about a Python codebase using retrieved source "
    "excerpts.\n"
    "- Ground every claim in the excerpts provided. Never invent names, files, "
    "or line numbers.\n"
    "- Cite sources as plain `path/to/file.py:42`, inline beside the claim and "
    "once each.\n"
    "- If the excerpts do not answer the question, say so plainly rather than "
    "guessing.\n"
    "- Be concise. Lead with the answer."
)


@dataclass
class Chunk:
    file: str
    start_line: int
    end_line: int
    text: str

    @property
    def location(self) -> Location:
        return Location(file=self.file, start_line=self.start_line, end_line=self.end_line)


@dataclass
class BaselineResult:
    question: str
    answer: str
    chunks: list[Chunk] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)

    @property
    def locations(self) -> list[Location]:
        return dedupe([chunk.location for chunk in self.chunks])


def chunk_file(relative_path: str, source: str) -> list[Chunk]:
    """Split one file into overlapping fixed-size line windows."""
    lines = source.splitlines()
    if not lines:
        return []

    step = max(CHUNK_LINES - CHUNK_OVERLAP, 1)
    chunks: list[Chunk] = []
    for start in range(0, len(lines), step):
        window = lines[start : start + CHUNK_LINES]
        if not window or not any(line.strip() for line in window):
            continue
        chunks.append(
            Chunk(
                file=relative_path,
                start_line=start + 1,
                end_line=start + len(window),
                text="\n".join(window),
            )
        )
        if start + CHUNK_LINES >= len(lines):
            break
    return chunks


class NaiveRAG:
    """Chunk, embed, retrieve top-k, answer."""

    def __init__(
        self,
        provider: LLMProvider,
        repo_path: Path | str,
        persist_dir: Path | str = ".chroma-baseline",
        top_k: int = TOP_K,
    ) -> None:
        self.provider = provider
        self.repo_path = Path(repo_path)
        self.top_k = top_k
        directory = Path(persist_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=str(directory), settings=Settings(anonymized_telemetry=False)
        )

    @staticmethod
    def _collection_name(repo_id: str) -> str:
        return "baseline_" + repo_id.replace("/", "__").replace(".", "_")

    def _collection(self, repo_id: str):
        return self._client.get_or_create_collection(
            name=self._collection_name(repo_id), metadata={"hnsw:space": "cosine"}
        )

    def index(self, repo_id: str, include_tests: bool = False, batch_size: int = 200) -> int:
        """Chunk and embed every Python file in the repository."""
        try:
            self._client.delete_collection(self._collection_name(repo_id))
        except Exception as exc:  # noqa: BLE001 - absent collection is not an error
            logger.debug("no baseline collection to drop for %s: %s", repo_id, exc)

        chunks: list[Chunk] = []
        for path in walk_files(self.repo_path, (".py",), include_tests=include_tests):
            relative = path.relative_to(self.repo_path).as_posix()
            source = path.read_text(encoding="utf-8", errors="replace")
            chunks.extend(chunk_file(relative, source))

        if not chunks:
            return 0

        collection = self._collection(repo_id)
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            collection.add(
                ids=[f"{c.file}:{c.start_line}" for c in batch],
                documents=[c.text for c in batch],
                metadatas=[
                    {"file": c.file, "start_line": c.start_line, "end_line": c.end_line}
                    for c in batch
                ],
            )
        return len(chunks)

    def retrieve(self, repo_id: str, question: str) -> list[Chunk]:
        collection = self._collection(repo_id)
        count = collection.count()
        if count == 0:
            return []

        found = collection.query(query_texts=[question], n_results=min(self.top_k, count))
        return [
            Chunk(
                file=str(metadata.get("file", "")),
                start_line=int(metadata.get("start_line") or 1),
                end_line=int(metadata.get("end_line") or 1),
                text=document,
            )
            for document, metadata in zip(
                found["documents"][0], found["metadatas"][0], strict=True
            )
        ]

    def answer(self, repo_id: str, question: str) -> BaselineResult:
        chunks = self.retrieve(repo_id, question)
        result = BaselineResult(question=question, answer="", chunks=chunks)

        if not chunks:
            result.answer = "No relevant source excerpts were retrieved."
            return result

        excerpts = "\n\n".join(
            f"--- {chunk.file}:{chunk.start_line}-{chunk.end_line} ---\n{chunk.text}"
            for chunk in chunks
        )
        response = self.provider.generate(
            f"Question: {question}\n\nRetrieved excerpts:\n{excerpts}\n\n"
            f"Answer the question from these excerpts.",
            system=_ANSWER_SYSTEM,
            max_tokens=ANSWER_MAX_TOKENS,
            effort="low",
        )
        result.usage.record("baseline_answer", response)
        result.answer = _tidy_answer(response.text)
        return result
