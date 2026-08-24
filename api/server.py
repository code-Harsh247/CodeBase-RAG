"""HTTP API behind the web UI.

Answering a question takes tens of seconds and several tool calls, so `/query`
streams: each hop is sent as it completes and the answer arrives last. Watching
the agent investigate is the point of the interface, not a loading spinner.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Iterator
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent.agent_loop import Hop, MultiHopAgent
from agent.provider import get_provider
from agent.query_agent import QueryAgent
from graph.neo4j_client import Neo4jClient
from graph.schema import SHARED_LABEL
from ingestion.pipeline import ingest
from ingestion.repo import DEFAULT_CLONE_ROOT, parse_github_url
from retrieval.tools import RetrievalTools

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


class QueryRequest(BaseModel):
    repo_id: str
    question: str = Field(min_length=1, max_length=2000)
    #: "multi_hop" investigates with tools; "single_hop" runs one Cypher query.
    mode: str = "multi_hop"


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


def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


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
                    tools = RetrievalTools(
                        client, request.repo_id, repo_path=_repo_path(request.repo_id)
                    )
                    result = MultiHopAgent(provider, tools, on_hop=on_hop).answer(
                        request.question
                    )
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
