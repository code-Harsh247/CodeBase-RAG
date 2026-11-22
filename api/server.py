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
import os
import queue
import secrets
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from agent.agent_loop import Hop, MultiHopAgent
from agent.metering import Budget, BudgetExhausted, MeteredProvider, RequestStopped
from agent.provider import LLMProvider, Message, ProviderBudgetError, get_provider
from agent.query_agent import QueryAgent
from agent.summarize import Turn, summarize, trim_to_budget
from api.auth import router as auth_router
from api.concurrency import QuerySlots
from api.deps import (
    CurrentUser,
    User,
    api_error,
    enforce_rate_limit,
    get_settings,
    get_store,
    same_origin,
)
from api.ingest_queue import IngestCoordinator, Ticket
from api.limiter import RateLimiter
from api.maintenance import evict, physical_delete, reconcile
from api.settings import Settings
from api.store import AppStore
from graph.neo4j_client import Neo4jClient
from ingestion.github import fetch_repo_meta
from ingestion.limits import IngestLimitError, IngestLimits
from ingestion.pipeline import ingest
from ingestion.repo import clone_root, parse_github_url, repo_dir, validate_repo_id
from retrieval.tools import RetrievalTools

logger = logging.getLogger(__name__)
# Neo4j logs a warning for every query naming a label or property that does
# not exist yet: on an empty database at startup, and whenever model-written
# Cypher has a typo. The agent already sees those as empty results.
logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

load_dotenv()

#: Every error a stream can end with. The UI keys its wording off ``code``;
#: ``message`` is a fixed, safe sentence. Exception text never leaves the
#: server: it can carry file paths, hostnames and query fragments.
ERROR_MESSAGES = {
    "internal": "Something went wrong on the server. Please try again.",
    "timeout": "That took too long and was stopped.",
    "budget_exhausted": "The demo has used its budget for today. It resets at 00:00 UTC.",
    "user_budget_exhausted": (
        "You have used your share of today's demo budget. It resets at 00:00 UTC."
    ),
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

#: How long a request waits for a repository another request is changing.
REPO_LOCK_WAIT_S = 5.0

#: Budget that must be left before a question or summary is even started:
#: roughly one agent turn's worst case, so a run is not refused on its first call.
MIN_QUERY_USD = 0.01
MIN_SUMMARIZE_USD = 0.003


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


def _drain(
    events: queue.Queue,
    cancel: threading.Event | None = None,
    deadline: float | None = None,
) -> Iterator[str]:
    """Forward queued events as SSE frames until the worker signals the end.

    ``cancel`` is set when the stream stops for any reason, including the
    client going away mid-stream, so the worker stops at its next LLM call
    instead of spending on an answer nobody will read. Past ``deadline`` the
    stream ends with a timeout even if the worker is stuck.
    """
    try:
        while True:
            try:
                item = events.get(timeout=1)
            except queue.Empty:
                if deadline is not None and time.monotonic() > deadline:
                    yield _event(_error("timeout"))
                    break
                continue
            if item is None:
                break
            yield _event(item)
    finally:
        if cancel is not None:
            cancel.set()


def get_budget(request: Request) -> Budget:
    return request.app.state.budget


def get_query_slots(request: Request) -> QuerySlots:
    return request.app.state.query_slots


def _over_budget(exc: BudgetExhausted) -> Exception:
    return api_error(429, exc.code, exc.message, headers={"Retry-After": str(exc.retry_after)})


def _stopped_event(exc: Exception) -> dict:
    """The SSE error for an exception that ended a question early, or ``internal``."""
    if isinstance(exc, BudgetExhausted):
        return _error(exc.code, exc.message)
    if isinstance(exc, RequestStopped):
        return _error(exc.code)
    if isinstance(exc, ProviderBudgetError):
        # The OpenRouter key itself is out of credit: the backstop tripped.
        return _error("budget_exhausted")
    return _error("internal")


def get_coordinator(request: Request) -> IngestCoordinator:
    return request.app.state.coordinator


def _busy() -> Exception:
    return api_error(
        409, "repo_busy", "That repository is being indexed right now. Try again shortly."
    )


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
    """One of the fixed error codes for an ingest failure; never exception text."""
    if isinstance(exc, IngestLimitError) and exc.code in ERROR_MESSAGES:
        return exc.code
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
    # Under the repository lock, so a removal cannot interleave with someone
    # adding the same repo, or with an ingest or eviction of it.
    try:
        with get_coordinator(request).repo_lock(repo_id, timeout_s=REPO_LOCK_WAIT_S):
            record = store.get_repo(repo_id)
            if record is not None and record.status == "indexing":
                raise _busy()

            if user.is_admin:
                # Includes graph data with no record at all, e.g. from before the store.
                with _client() as client:
                    result = physical_delete(client, record.repo_id if record else repo_id, store)
                return {**result, "scope": "repo"}

            if record is None or not store.is_owner(user.github_id, record.repo_id):
                raise _not_found()

            remaining = store.remove_owner(user.github_id, record.repo_id)
            if remaining == 0 and not record.public:
                with _client() as client:
                    result = physical_delete(client, record.repo_id, store)
                return {**result, "scope": "repo"}
            return {"repo_id": record.repo_id, "scope": "claim"}
    except TimeoutError:
        raise _busy() from None


@router.post("/summarize", dependencies=[Depends(same_origin)])
def summarize_history(body: SummarizeRequest, request: Request, user: User) -> SummarizeResponse:
    """Fold recent turns into the thread's running summary.

    Called in the background after an answer lands, so the next question pays
    no latency for it — see the frontend's `ask()`. Metered like any other
    LLM call; when the budget is gone the client keeps its unsummarized turns.
    """
    enforce_rate_limit(request, "summarize", user)
    budget = get_budget(request)
    try:
        budget.ensure_available(user.github_id, MIN_SUMMARIZE_USD)
        provider = MeteredProvider(get_provider(), budget, user.github_id)
        summary, _ = summarize(
            provider,
            body.prior_summary,
            [Turn(turn.role, turn.content) for turn in body.turns],
        )
    except BudgetExhausted as exc:
        raise _over_budget(exc) from None
    except ProviderBudgetError:
        logger.error("OpenRouter is out of credit")
        raise api_error(429, "budget_exhausted", ERROR_MESSAGES["budget_exhausted"]) from None
    return SummarizeResponse(summary=summary)


def _stream_answer(
    request: QueryRequest,
    repo_id: str,
    provider: LLMProvider,
    cancel: threading.Event,
    deadline: float,
    on_finish: Callable[[], None] | None = None,
) -> Iterator[str]:
    """Run the agent on a worker thread, forwarding hops as they land.

    The agent is synchronous and its callback fires mid-run, so the work runs
    off-thread and communicates through a queue; the generator drains that
    queue, which is what turns a 30-second call into a live trace.

    ``provider`` is metered: it refuses calls once the budget is spent,
    ``cancel`` is set, or ``deadline`` passes, which is how an abandoned or
    runaway question stops costing money.

    Not itself a generator: the worker starts *now* and ``on_finish`` runs
    when it ends, even if the client disconnects before the stream is read
    (a generator's cleanup would never run in that case).
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
        except (BudgetExhausted, RequestStopped, ProviderBudgetError) as exc:
            logger.info("query on %s stopped: %s", repo_id, type(exc).__name__)
            events.put(_stopped_event(exc))
        except Exception:
            logger.exception("query failed for %s", repo_id)
            events.put(_error("internal"))
        finally:
            events.put(None)
            if on_finish is not None:
                on_finish()

    threading.Thread(target=work, daemon=True).start()
    # A little grace past the provider's own deadline, for the last call to land.
    return _drain(events, cancel, deadline=deadline + 30)


@router.post("/query", dependencies=[Depends(same_origin)])
def query(body: QueryRequest, request: Request, user: User) -> StreamingResponse:
    store = get_store(request)
    settings = get_settings(request)
    budget = get_budget(request)
    # Every check happens before streaming: once the stream starts the
    # response is a 200. Cheapest and most abuse-relevant first.
    enforce_rate_limit(request, "query", user)
    if not store.can_access(user.github_id, body.repo_id):
        raise _not_found()
    try:
        budget.ensure_available(user.github_id, MIN_QUERY_USD)
    except BudgetExhausted as exc:
        raise _over_budget(exc) from None

    record = store.get_repo(body.repo_id)
    store.touch_repo(record.repo_id)
    cancel = threading.Event()
    deadline = time.monotonic() + settings.query_deadline_s
    provider = MeteredProvider(
        get_provider(),
        budget,
        user.github_id,
        prices=(settings.llm_price_input_per_m, settings.llm_price_output_per_m),
        cancel=cancel,
        deadline=deadline,
    )
    slot, reason = get_query_slots(request).try_acquire(user.github_id)
    if slot is None:
        if reason == "user_busy":
            raise api_error(
                429, "user_busy", "You already have a question running. Wait for it to finish."
            )
        raise api_error(
            503,
            "busy",
            "The server is answering as many questions as it can. Try again in a moment.",
            headers={"Retry-After": "15"},
        )
    try:
        stream = _stream_answer(
            body, record.repo_id, provider, cancel, deadline, on_finish=slot.release
        )
    except BaseException:
        slot.release()
        raise
    return _stream(stream)


def _ensure_disk(settings: Settings) -> None:
    root = clone_root()
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < settings.min_free_disk_gb * 1e9:
        raise IngestLimitError("server_full", ERROR_MESSAGES["server_full"])


def _limits(settings: Settings) -> IngestLimits:
    return IngestLimits(
        max_files=settings.max_py_files,
        max_total_bytes=settings.max_py_total_mb * 1_000_000,
        max_graph_nodes=settings.max_graph_nodes,
        clone_timeout_s=settings.clone_timeout_s,
        max_clone_disk_bytes=settings.max_clone_disk_mb * 1_000_000,
        deadline=time.monotonic() + settings.ingest_deadline_s,
    )


def _stream_ingest(
    app: FastAPI,
    user: CurrentUser,
    repo_id: str,
    *,
    refresh: bool,
    fresh: bool,
    ticket: Ticket,
) -> Iterator[str]:
    """Wait in line, then clone, parse and index, reporting each stage.

    The caller has claimed ``repo_id`` with ``begin_indexing`` and taken a
    queue ticket; this always settles both. A failed *fresh* ingest leaves
    nothing behind — no record, graph, index or clone — so it cannot hold
    disk or show up half-built. A failed refresh is marked failed for an
    admin to look at.
    """
    settings: Settings = app.state.settings
    store: AppStore = app.state.store
    coordinator: IngestCoordinator = app.state.coordinator
    events: queue.Queue = queue.Queue()
    cancel = threading.Event()

    def queued(ahead: int) -> None:
        events.put(
            {
                "type": "progress",
                "stage": "queued",
                "detail": f"{ahead} indexing job{'s' if ahead != 1 else ''} ahead of you",
            }
        )

    def settle_failure(client) -> None:
        if not fresh:
            store.mark_failed(repo_id)
            return
        try:
            if client is not None:
                physical_delete(client, repo_id, store)
            else:
                store.forget_repo(repo_id)
        except Exception:
            logger.warning("cleanup after failed ingest of %s failed", repo_id, exc_info=True)
            store.mark_failed(repo_id)

    def work() -> None:
        settled = False
        try:
            events.put({"type": "progress", "stage": "start", "detail": repo_id})
            # Only waiting is cancellable: once indexing starts it runs to the
            # end, since the result is shared by everyone who adds this repo.
            ticket.wait(cancel, on_position=queued)
            with _client() as client, coordinator.repo_lock(repo_id):
                try:
                    evict(
                        store,
                        client,
                        coordinator,
                        max_indexed=settings.max_indexed_repos,
                        idle_ttl_s=(
                            settings.repo_idle_ttl_days * 86_400
                            if settings.repo_idle_ttl_days
                            else None
                        ),
                        keep=repo_id,
                    )
                    _ensure_disk(settings)
                    summary = ingest(
                        f"https://github.com/{repo_id}",
                        client,
                        refresh=refresh,
                        on_progress=lambda stage, detail: events.put(
                            {"type": "progress", "stage": stage, "detail": detail}
                        ),
                        # Never index a path on this machine, whatever the input says.
                        allow_local=False,
                        limits=_limits(settings),
                    )
                except Exception:
                    settle_failure(client)
                    settled = True
                    raise
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
            logger.info("ingest of %s stopped: %s", repo_id, exc.code)
            if not settled:
                settle_failure(None)
            events.put(_error(_failure_code(exc), exc.message))
        except Exception:
            logger.exception("ingest of %s failed", repo_id)
            if not settled:
                settle_failure(None)
            events.put(_error("internal"))
        finally:
            ticket.release()
            events.put(None)

    # Started now, not when the stream is first read: the worker is what
    # settles the queue ticket and the `indexing` claim, so it must run even
    # if the client never reads a byte.
    threading.Thread(target=work, daemon=True).start()
    return _drain(events, cancel)


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


def _add_existing(request: Request, user: CurrentUser, repo_id: str):
    """Add an already-indexed repo to the user's projects; the record, or None.

    Under the repository lock, re-checking it is still ready, so this cannot
    race the last owner deleting it.
    """
    store = get_store(request)
    try:
        with get_coordinator(request).repo_lock(repo_id, timeout_s=REPO_LOCK_WAIT_S):
            record = store.get_repo(repo_id)
            if record is None or record.status != "ready":
                return None
            store.add_owner(user.github_id, record.repo_id)
            store.touch_repo(record.repo_id)
            return record
    except TimeoutError:
        raise _busy() from None


def _stream(body: Iterator[str]) -> StreamingResponse:
    return StreamingResponse(body, media_type="text/event-stream", headers=_STREAM_HEADERS)


@router.post("/ingest", dependencies=[Depends(same_origin)])
def ingest_repo(body: IngestRequest, request: Request, user: User) -> StreamingResponse:
    """Index a public GitHub repository, or add an already-indexed one.

    Every refusal happens here, before streaming starts, so it arrives as a
    proper HTTP status: once the stream begins the response is already a 200.
    """
    settings = get_settings(request)
    store = get_store(request)
    coordinator = get_coordinator(request)

    try:
        owner, name = parse_github_url(body.url)
    except ValueError as exc:
        raise api_error(400, "invalid_url", str(exc)) from None
    repo_id = f"{owner}/{name}"
    refresh = body.refresh and user.is_admin

    def owns(candidate: str) -> bool:
        return store.is_owner(user.github_id, candidate)

    def check_quota(candidate: str) -> None:
        if user.is_admin or owns(candidate):
            return
        if store.owned_count(user.github_id) >= settings.max_repos_per_user:
            raise api_error(
                403,
                "repo_limit",
                f"You can have up to {settings.max_repos_per_user} projects. "
                f"Remove one to add another.",
            )

    # Already indexed by someone: adding it costs nothing.
    record = store.get_repo(repo_id)
    if record is not None and record.status == "ready" and not refresh:
        check_quota(record.repo_id)
        enforce_rate_limit(request, "add", user)
        added = _add_existing(request, user, record.repo_id)
        if added is not None:
            return _stream(_already_indexed(added))

    check_quota(repo_id)
    # Counted before the GitHub lookup, so probing for sizes is limited too.
    enforce_rate_limit(request, "ingest", user)

    # Ask GitHub before cloning: existence, size, and the canonical spelling.
    try:
        meta = fetch_repo_meta(
            owner,
            name,
            token=settings.github_token,
            transport=request.app.state.github_transport,
        )
    except IngestLimitError as exc:
        raise api_error(404, exc.code, exc.message) from None
    if meta is not None:
        if meta.size_kb > settings.max_repo_size_mb * 1024:
            raise api_error(
                413,
                "repo_too_large",
                f"That repository is about {meta.size_kb // 1024} MB; this demo indexes "
                f"repositories up to {settings.max_repo_size_mb} MB.",
            )
        repo_id = meta.full_name
        # A renamed repo may already be indexed under its current name.
        record = store.get_repo(repo_id)
        if record is not None and record.status == "ready" and not refresh:
            added = _add_existing(request, user, record.repo_id)
            if added is not None:
                return _stream(_already_indexed(added))
    if record is not None:
        repo_id = record.repo_id

    ticket = coordinator.try_enqueue()
    if ticket is None:
        raise api_error(
            503,
            "ingest_busy",
            "The server is busy indexing other repositories. Try again in a few minutes.",
            headers={"Retry-After": "60"},
        )
    if not store.begin_indexing(repo_id):
        ticket.release()
        raise _busy()

    fresh = record is None or record.status != "ready"
    return _stream(
        _stream_ingest(
            request.app, user, repo_id, refresh=refresh, fresh=fresh, ticket=ticket
        )
    )


# --------------------------------------------------------------------- app


def _startup(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    workers = os.environ.get("WEB_CONCURRENCY", "1")
    if workers.isdigit() and int(workers) > 1:
        # Rate limits, budget reservations, query slots and the ingest line
        # all live in this process; several workers would each enforce them
        # separately and multiply every limit.
        logger.warning("WEB_CONCURRENCY=%s: run a single worker, or limits multiply", workers)
    store = AppStore(settings.app_db_path)
    app.state.store = store
    app.state.budget = Budget(
        store,
        daily_usd=settings.daily_budget_usd,
        per_user_usd=settings.per_user_daily_usd,
    )
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
    app.state.coordinator = IngestCoordinator(workers=1, max_waiting=settings.ingest_queue_max)
    app.state.limiter = RateLimiter()
    app.state.query_slots = QuerySlots(max_total=settings.max_concurrent_queries, per_user=1)
    # Keys the HMAC of client IPs. Without APP_SECRET (development only, see
    # Settings.check) a per-process secret still keeps raw IPs out of memory.
    app.state.ip_secret = settings.app_secret.encode() or secrets.token_bytes(32)

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

    # In production the built UI is served from here too: one origin, so no
    # CORS and the session cookie just works. In development Vite serves it.
    ui_dist = os.environ.get("UI_DIST")
    if ui_dist and Path(ui_dist, "index.html").is_file():
        app.mount("/", StaticFiles(directory=ui_dist, html=True), name="ui")
    return app


app = create_app()
