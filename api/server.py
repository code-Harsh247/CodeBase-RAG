"""HTTP API behind the web UI.

Answering a question takes tens of seconds and several tool calls, so `/query`
streams: each hop is sent as it completes and the answer arrives last. Watching
the agent investigate is the point of the interface, not a loading spinner.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import stat
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent.agent_loop import Hop, MultiHopAgent
from agent.provider import Message, get_provider
from agent.query_agent import QueryAgent
from agent.summarize import Turn, summarize, trim_to_budget
from graph.neo4j_client import Neo4jClient
from graph.schema import SHARED_LABEL
from ingestion.pipeline import ingest
from ingestion.repo import DEFAULT_CLONE_ROOT, parse_github_url
from retrieval.tools import RetrievalTools
from retrieval.vector_store import VectorStore

logger = logging.getLogger(__name__)

load_dotenv()

app = FastAPI(title="CodeGraph API", version="0.1.0")

# The UI is served from a separate origin in development (Vite on :5173).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class HistoryTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class QueryRequest(BaseModel):
    repo_id: str
    question: str = Field(min_length=1, max_length=2000)
    #: "multi_hop" investigates with tools; "single_hop" runs one Cypher query.
    mode: str = "multi_hop"
    #: Earlier turns of this thread that have not been folded into
    #: `history_summary` yet. Bounded again server-side before use — see
    #: `trim_to_budget` — since the client's fold-in may still be in flight.
    history: list[HistoryTurn] = Field(default_factory=list, max_length=20)
    #: Running summary of the turns before those, or "" for a fresh thread.
    history_summary: str = Field(default="", max_length=4000)


class SummarizeRequest(BaseModel):
    prior_summary: str = Field(default="", max_length=4000)
    turns: list[HistoryTurn] = Field(min_length=1, max_length=20)


class SummarizeResponse(BaseModel):
    summary: str


class IngestRequest(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    #: Re-clone and rebuild even if the repository is already indexed.
    refresh: bool = False


def _client() -> Neo4jClient:
    client = Neo4jClient.from_env()
    client.verify_connectivity()
    return client


@app.get("/api/health")
def health() -> dict:
    try:
        with _client():
            graph_ok = True
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        logger.warning("health check: graph unreachable: %s", exc)
        graph_ok = False
    return {"ok": True, "graph": graph_ok}


@app.get("/api/repos")
def repos() -> dict:
    """Repositories present in the graph, with their node counts."""
    with _client() as client:
        rows = client.run(
            f"MATCH (n:{SHARED_LABEL}) WHERE n.repo_id IS NOT NULL "
            f"RETURN n.repo_id AS repo_id, count(n) AS nodes "
            f"ORDER BY repo_id"
        )
    return {
        "repos": [
            {
                "repo_id": row["repo_id"],
                "nodes": row["nodes"],
                "has_source": (DEFAULT_CLONE_ROOT / row["repo_id"]).exists(),
            }
            for row in rows
        ]
    }


def _repo_path(repo_id: str) -> Path | None:
    candidate = DEFAULT_CLONE_ROOT / repo_id
    return candidate if candidate.exists() else None


def _force_remove(func, path, _exc) -> None:
    """Clear the read-only bit and retry.

    Git marks files under `.git/objects` read-only, which makes `rmtree` fail
    on Windows. Passing `ignore_errors` instead would leave the clone on disk
    and report success, which is worse than failing loudly.
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


@app.delete("/api/repos/{owner}/{name}")
def delete_repo(owner: str, name: str) -> dict:
    """Remove a repository: its graph nodes, its embeddings, and its clone.

    Deleting only the browser's history would not work — the repository would
    reappear from /api/repos on the next refresh — so this removes the thing
    itself. It is destructive and irreversible; re-adding means re-indexing.
    """
    repo_id = f"{owner}/{name}"

    # `owner` and `name` come from the URL path. Resolve and confirm the result
    # is still inside the clone root before deleting anything from disk.
    root = DEFAULT_CLONE_ROOT.resolve()
    target = (DEFAULT_CLONE_ROOT / owner / name).resolve()
    if root not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid repository id")

    with _client() as client:
        removed = client.delete_repo(repo_id)

    try:
        VectorStore().drop(repo_id)
    except Exception as exc:  # noqa: BLE001 - the graph is already gone
        logger.warning("could not drop embeddings for %s: %s", repo_id, exc)

    source_removed = False
    if target.is_dir():
        shutil.rmtree(target, onexc=_force_remove)
        source_removed = not target.exists()

    logger.info("deleted %s: %d nodes, source=%s", repo_id, removed, source_removed)
    return {"repo_id": repo_id, "nodes_removed": removed, "source_removed": source_removed}


def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _history_messages(request: QueryRequest) -> list[Message]:
    """Prior turns of this thread, as messages the agent can prepend.

    The running summary comes first as a system message, then whatever recent
    turns have not been folded into it yet — trimmed to a real token budget,
    because these are re-sent on every hop and the client's fold-in may not
    have caught up.
    """
    messages: list[Message] = []
    if request.history_summary:
        messages.append(
            Message(
                role="system",
                content=f"Earlier in this conversation: {request.history_summary}",
            )
        )
    turns = trim_to_budget([Turn(t.role, t.content) for t in request.history])
    messages.extend(Message(role=turn.role, content=turn.content) for turn in turns)
    return messages


@app.post("/api/summarize")
def summarize_history(request: SummarizeRequest) -> SummarizeResponse:
    """Fold recent turns into the thread's running summary.

    Called in the background after an answer lands, so the next question pays
    no latency for it — see the frontend's `ask()`.
    """
    provider = get_provider()
    summary, _ = summarize(
        provider,
        request.prior_summary,
        [Turn(turn.role, turn.content) for turn in request.turns],
    )
    return SummarizeResponse(summary=summary)


def _stream_answer(request: QueryRequest) -> Iterator[str]:
    """Run the agent on a worker thread, forwarding hops as they land.

    The agent is synchronous and its callback fires mid-run, so the work runs
    off-thread and communicates through a queue; the generator drains that
    queue, which is what turns a 30-second call into a live trace.
    """
    events: queue.Queue = queue.Queue()

    def on_hop(number: int, hop: Hop) -> None:
        events.put(
            {
                "type": "hop",
                "n": number,
                "tool": hop.tool,
                "argument": hop.argument,
                "ok": hop.ok,
                "locations": [str(item) for item in hop.locations],
                "preview": hop.result[:400],
            }
        )

    def work() -> None:
        try:
            with _client() as client:
                provider = get_provider()
                if request.mode == "single_hop":
                    result = QueryAgent(provider, client, request.repo_id).answer(
                        request.question
                    )
                    for attempt in result.attempts:
                        events.put(
                            {
                                "type": "hop",
                                "n": 1,
                                "tool": "graph_query",
                                "argument": attempt.cypher,
                                "ok": attempt.ok,
                                "locations": [],
                                "preview": attempt.error or "",
                            }
                        )
                else:

                    def on_answer_delta(text: str) -> None:
                        events.put({"type": "answer_delta", "text": text})

                    tools = RetrievalTools(
                        client, request.repo_id, repo_path=_repo_path(request.repo_id)
                    )
                    result = MultiHopAgent(
                        provider, tools, on_hop=on_hop, on_answer_delta=on_answer_delta
                    ).answer(request.question, history=_history_messages(request))
                events.put(
                    {
                        "type": "answer",
                        "answer": result.answer,
                        "locations": [str(item) for item in result.locations],
                        "usage": {
                            "calls": result.usage.calls,
                            "tokens": result.usage.total_tokens,
                        },
                    }
                )
        except Exception as exc:
            logger.exception("query failed")
            events.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            events.put(None)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()

    while True:
        item = events.get()
        if item is None:
            break
        yield _event(item)


def _readable_failure(exc: Exception) -> str:
    """Turn a clone failure into something a person can act on.

    A raw GitCommandError is several lines of command line and stderr; the
    part that matters is almost always "that repository is not reachable".
    """
    text = str(exc)
    if "Repository not found" in text or "not found" in text.lower():
        return (
            "Repository not found. Check the URL, and note that private "
            "repositories are not supported."
        )
    if "could not resolve host" in text.lower() or "unable to access" in text.lower():
        return "Could not reach GitHub. Check your network connection."
    return f"{type(exc).__name__}: {text[:300]}"


def _stream_ingest(request: IngestRequest) -> Iterator[str]:
    """Clone, parse and index a repository, reporting each stage as it starts."""
    events: queue.Queue = queue.Queue()

    def work() -> None:
        try:
            # Reject anything that is not a GitHub repository before cloning:
            # this endpoint runs `git clone` on whatever it is given.
            owner, name = parse_github_url(request.url)
            events.put({"type": "progress", "stage": "start", "detail": f"{owner}/{name}"})

            with _client() as client:
                summary = ingest(
                    request.url,
                    client,
                    refresh=request.refresh,
                    on_progress=lambda stage, detail: events.put(
                        {"type": "progress", "stage": stage, "detail": detail}
                    ),
                )
            events.put(
                {
                    "type": "done",
                    "repo_id": summary.repo_id,
                    "files": summary.files_parsed,
                    "nodes": sum(summary.node_counts.values()),
                    "edges": sum(summary.edge_counts.values()),
                    "embedded": summary.embedded,
                    "seconds": round(summary.duration_seconds, 1),
                }
            )
        except ValueError as exc:
            events.put({"type": "error", "message": str(exc)})
        except Exception as exc:
            logger.exception("ingest failed")
            events.put({"type": "error", "message": _readable_failure(exc)})
        finally:
            events.put(None)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()

    while True:
        item = events.get()
        if item is None:
            break
        yield _event(item)


@app.post("/api/ingest")
def ingest_repo(request: IngestRequest) -> StreamingResponse:
    return StreamingResponse(
        _stream_ingest(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/query")
def query(request: QueryRequest) -> StreamingResponse:
    if request.mode not in ("multi_hop", "single_hop"):
        raise HTTPException(status_code=400, detail="mode must be multi_hop or single_hop")
    return StreamingResponse(
        _stream_answer(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
