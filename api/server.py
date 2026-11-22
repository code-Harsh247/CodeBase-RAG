"""HTTP API behind the web UI.

Answering a question takes tens of seconds and several tool calls, so `/query`
streams: each hop is sent as it completes and the answer arrives last. Watching
the agent investigate is the point of the interface, not a loading spinner.

Every route except `/api/health` and the sign-in flow requires a GitHub login.
Each user sees the repositories they indexed plus any an admin made public;
see `delete_repo` for who may remove what.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from agent.agent_loop import Hop, MultiHopAgent
from agent.provider import Message, get_provider
from agent.query_agent import QueryAgent
from agent.summarize import Turn, summarize, trim_to_budget
from api.auth import router as auth_router
from api.deps import CurrentUser, User, api_error, get_store, same_origin
from api.maintenance import physical_delete, reconcile
from api.settings import Settings
from api.store import AppStore
from graph.neo4j_client import Neo4jClient
from ingestion.limits import IngestLimitError
from ingestion.pipeline import ingest
from ingestion.repo import parse_github_url, repo_dir, validate_repo_id
from retrieval.tools import RetrievalTools

logger = logging.getLogger(__name__)

load_dotenv()

#: Every error a stream can end with. The UI keys its wording off ``code``;
#: ``message`` is a fixed, safe sentence. Exception text never leaves the
#: server: it can carry file paths, hostnames and query fragments.
ERROR_MESSAGES = {
    "internal": "Something went wrong on the server. Please try again.",
    "timeout": "That took too long and was stopped.",
    "budget_exhausted": "The demo has used its budget for today. Try again tomorrow.",
    "repo_too_large": "That repository is too large for this demo.",
    "no_python_files": "That repository has no Python source to index.",
    "clone_failed": "Could not download the repository from GitHub.",
    "repo_not_found": (
        "Repository not found. Check the URL; private repositories are not supported."
    ),
    "server_full": "The server is out of space for new repositories right now.",
    "cancelled": "The request was cancelled.",
}

_STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _error(code: str, message: str | None = None) -> dict:
    return {"type": "error", "code": code, "message": message or ERROR_MESSAGES[code]}


def _checked_repo_id(value: str) -> str:
    owner, name = validate_repo_id(value)
    return f"{owner}/{name}"


def _not_found() -> Exception:
    # Same answer for "does not exist" and "not yours": which repositories
    # other people indexed is not something to reveal.
    return api_error(404, "repo_not_found", "No such repository in your projects.")


# ------------------------------------------------------------------ models


class HistoryTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class QueryRequest(BaseModel):
    repo_id: str = Field(min_length=3, max_length=141)
    question: str = Field(min_length=1, max_length=2000)
    #: "multi_hop" investigates with tools; "single_hop" runs one Cypher query.
    mode: Literal["multi_hop", "single_hop"] = "multi_hop"
    #: Earlier turns of this thread that have not been folded into
    #: `history_summary` yet. Bounded again server-side before use — see
    #: `trim_to_budget` — since the client's fold-in may still be in flight.
    history: list[HistoryTurn] = Field(default_factory=list, max_length=20)
    #: Running summary of the turns before those, or "" for a fresh thread.
    history_summary: str = Field(default="", max_length=4000)

    # The repo id becomes a filesystem path for grep/read_code, so a malformed
    # one is refused here (422) rather than trusted downstream.
    @field_validator("repo_id")
    @classmethod
    def _valid_repo_id(cls, value: str) -> str:
        return _checked_repo_id(value)


class SummarizeRequest(BaseModel):
    prior_summary: str = Field(default="", max_length=4000)
    turns: list[HistoryTurn] = Field(min_length=1, max_length=20)


class SummarizeResponse(BaseModel):
    summary: str


class IngestRequest(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    #: Re-clone and rebuild even if already indexed. Honoured for admins only.
    refresh: bool = False


# ----------------------------------------------------------------- helpers


def _client() -> Neo4jClient:
    client = Neo4jClient.from_env()
    client.verify_connectivity()
    return client


def _repo_path(repo_id: str) -> Path | None:
    try:
        candidate = repo_dir(repo_id)
    except ValueError:
        return None
    return candidate if candidate.is_dir() else None


def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _drain(events: queue.Queue) -> Iterator[str]:
    while True:
        item = events.get()
        if item is None:
            break
        yield _event(item)


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


def _failure_code(exc: Exception) -> str:
    """Classify an ingest failure into one of the fixed error codes.

    A raw GitCommandError is several lines of command line and stderr; the
    part that matters is almost always "that repository is not reachable".
    """
    if isinstance(exc, IngestLimitError):
        return exc.code if exc.code in ERROR_MESSAGES else "internal"
    text = str(exc).lower()
    if "repository not found" in text or "not found" in text:
        return "repo_not_found"
    if "git" in type(exc).__module__.lower() or "could not resolve host" in text:
        return "clone_failed"
    return "internal"


# ------------------------------------------------------------------ routes

router = APIRouter(prefix="/api")


@router.get("/health")
def health() -> dict:
    try:
        with _client():
            graph_ok = True
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        logger.warning("health check: graph unreachable: %s", exc)
        graph_ok = False
    return {"ok": True, "graph": graph_ok}


@router.get("/repos")
def repos(request: Request, user: User) -> dict:
    """The repositories this user can open: their own, plus public ones."""
    return {
        "repos": [
            {
                "repo_id": record.repo_id,
                "nodes": record.nodes,
                "files": record.files,
                "has_source": _repo_path(record.repo_id) is not None,
                "public": record.public,
                "owned": record.owned,
            }
            for record in get_store(request).visible_repos(user.github_id)
        ]
    }


@router.delete("/repos/{owner}/{name}", dependencies=[Depends(same_origin)])
def delete_repo(
    owner: str, name: str, request: Request, user: User
) -> dict:
    """Remove a repository from this user's projects.

    Several people can own the same indexed repository (indexing one that
    exists just adds a claim), so for a regular user this drops *their* claim,
    and the data itself goes only once nobody owns it and it is not public.
    Admins delete outright. Nobody can remove a repository they do not own.
    """
    try:
        repo_id = _checked_repo_id(f"{owner}/{name}")
    except ValueError:
        raise api_error(400, "invalid_repo", "Invalid repository id.") from None

    store = get_store(request)
    record = store.get_repo(repo_id)

    if user.is_admin:
        # Includes graph data with no record at all, e.g. from before the store.
        with _client() as client:
            result = physical_delete(client, record.repo_id if record else repo_id, store)
        return {**result, "scope": "repo"}

    if record is None or not store.is_owner(user.github_id, record.repo_id):
        raise _not_found()

    remaining = store.remove_owner(user.github_id, record.repo_id)
    if remaining == 0 and not record.public and record.status != "indexing":
        with _client() as client:
            result = physical_delete(client, record.repo_id, store)
        return {**result, "scope": "repo"}
    return {"repo_id": record.repo_id, "scope": "claim"}


@router.post("/summarize", dependencies=[Depends(same_origin)])
def summarize_history(
    request: SummarizeRequest, user: User
) -> SummarizeResponse:
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


def _stream_answer(request: QueryRequest, repo_id: str) -> Iterator[str]:
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
                    result = QueryAgent(provider, client, repo_id).answer(request.question)
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

                    tools = RetrievalTools(client, repo_id, repo_path=_repo_path(repo_id))
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
        except Exception:
            logger.exception("query failed for %s", repo_id)
            events.put(_error("internal"))
        finally:
            events.put(None)

    threading.Thread(target=work, daemon=True).start()
    yield from _drain(events)


@router.post("/query", dependencies=[Depends(same_origin)])
def query(
    body: QueryRequest, request: Request, user: User
) -> StreamingResponse:
    store = get_store(request)
    # Checked before streaming: once the stream starts the response is a 200.
    if not store.can_access(user.github_id, body.repo_id):
        raise _not_found()
    record = store.get_repo(body.repo_id)
    store.touch_repo(record.repo_id)
    return StreamingResponse(
        _stream_answer(body, record.repo_id),
        media_type="text/event-stream",
        headers=_STREAM_HEADERS,
    )


def _stream_ingest(
    store: AppStore, user: CurrentUser, repo_id: str, refresh: bool
) -> Iterator[str]:
    """Clone, parse and index a repository, reporting each stage as it starts.

    The caller has already claimed ``repo_id`` with ``begin_indexing``; this
    always settles that claim as ready or failed.
    """
    events: queue.Queue = queue.Queue()

    def work() -> None:
        try:
            events.put({"type": "progress", "stage": "start", "detail": repo_id})
            with _client() as client:
                summary = ingest(
                    f"https://github.com/{repo_id}",
                    client,
                    refresh=refresh,
                    on_progress=lambda stage, detail: events.put(
                        {"type": "progress", "stage": stage, "detail": detail}
                    ),
                    # Never index a path on this machine, whatever the input says.
                    allow_local=False,
                )
            nodes = sum(summary.node_counts.values())
            store.mark_ready(
                repo_id, commit_sha=summary.commit, files=summary.files_parsed, nodes=nodes
            )
            store.add_owner(user.github_id, repo_id)
            events.put(
                {
                    "type": "done",
                    "repo_id": repo_id,
                    "files": summary.files_parsed,
                    "nodes": nodes,
                    "edges": sum(summary.edge_counts.values()),
                    "embedded": summary.embedded,
                    "seconds": round(summary.duration_seconds, 1),
                }
            )
        except IngestLimitError as exc:
            # Limit messages are written for users and carry no internals.
            logger.info("ingest of %s refused: %s", repo_id, exc.code)
            store.mark_failed(repo_id)
            events.put(_error(_failure_code(exc), exc.message))
        except Exception as exc:
            logger.exception("ingest of %s failed", repo_id)
            store.mark_failed(repo_id)
            events.put(_error(_failure_code(exc)))
        finally:
            events.put(None)

    threading.Thread(target=work, daemon=True).start()
    yield from _drain(events)


def _already_indexed(record) -> Iterator[str]:
    yield _event({"type": "progress", "stage": "start", "detail": record.repo_id})
    yield _event(
        {
            "type": "done",
            "repo_id": record.repo_id,
            "files": record.files,
            "nodes": record.nodes,
            "edges": 0,
            "embedded": 0,
            "seconds": 0,
            "cached": True,
        }
    )


@router.post("/ingest", dependencies=[Depends(same_origin)])
def ingest_repo(
    body: IngestRequest, request: Request, user: User
) -> StreamingResponse:
    # Reject anything that is not a GitHub repository before streaming starts:
    # this endpoint runs `git clone` on what it is given, and once the stream
    # begins the response is already a 200.
    try:
        owner, name = parse_github_url(body.url)
    except ValueError as exc:
        raise api_error(400, "invalid_url", str(exc)) from None

    store = get_store(request)
    record = store.get_repo(f"{owner}/{name}")
    repo_id = record.repo_id if record else f"{owner}/{name}"
    refresh = body.refresh and user.is_admin

    # Somebody already indexed it: adding it to this user's projects is free.
    if record is not None and record.status == "ready" and not refresh:
        store.add_owner(user.github_id, repo_id)
        store.touch_repo(repo_id)
        return StreamingResponse(
            _already_indexed(record), media_type="text/event-stream", headers=_STREAM_HEADERS
        )

    if not store.begin_indexing(repo_id):
        raise api_error(
            409, "ingest_busy", "That repository is being indexed right now. Try again shortly."
        )
    return StreamingResponse(
        _stream_ingest(store, user, repo_id, refresh),
        media_type="text/event-stream",
        headers=_STREAM_HEADERS,
    )


# --------------------------------------------------------------------- app


def _startup(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    store = AppStore(settings.app_db_path)
    app.state.store = store
    purged = store.purge_expired_sessions()
    try:
        with _client() as client:
            counts = reconcile(store, client)
        logger.info("reconciled repos with the graph: %s", counts)
    except Exception as exc:  # noqa: BLE001 - the API can start without the graph
        logger.warning("could not reconcile with the graph: %s", exc)
    # PUBLIC_REPOS, when set, is the source of truth for what is public.
    if settings.public_repos:
        store.apply_public(settings.public_repos)
    logger.info("app store ready at %s (%d expired sessions purged)", settings.app_db_path, purged)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _startup(app)
    try:
        yield
    finally:
        app.state.store.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(
        title="CodeGraph API",
        version="0.1.0",
        lifespan=_lifespan,
        # The interactive docs are a map of the attack surface; keep them local.
        docs_url=None if settings.production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.production else "/openapi.json",
    )
    app.state.settings = settings
    app.state.github_transport = None

    # The UI is served from a separate origin only when not behind the Vite
    # proxy; in production it is same-origin, so this list is usually empty.
    if settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.allowed_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Content-Type"],
        )

    app.include_router(auth_router)
    app.include_router(router)
    return app


app = create_app()
