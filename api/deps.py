"""FastAPI dependencies: who is asking, and is the request allowed to act."""

from __future__ import annotations

import math
import zlib
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from api.limiter import RateLimiter, Rule, client_ip_key, parse_rules
from api.settings import Settings
from api.store import AppStore

SESSION_COOKIE = "cg_session"


def api_error(status: int, code: str, message: str, headers: dict | None = None) -> HTTPException:
    """Errors carry a stable ``code`` the UI keys its wording off."""
    return HTTPException(status_code=status, detail={"code": code, "message": message}, headers=headers)


@dataclass(frozen=True)
class CurrentUser:
    github_id: int
    login: str
    avatar_url: str
    is_admin: bool


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_store(request: Request) -> AppStore:
    return request.app.state.store


def _dev_github_id(login: str) -> int:
    # Negative, so it can never collide with a real GitHub account id.
    return -(zlib.crc32(login.lower().encode()) or 1)


def current_user(request: Request) -> CurrentUser:
    """The signed-in user, or 401 ``login_required``; banned users get 403."""
    settings = get_settings(request)
    store = get_store(request)

    if settings.auth_dev_user and not settings.production:
        user = store.get_user(_dev_github_id(settings.auth_dev_user)) or store.upsert_user(
            _dev_github_id(settings.auth_dev_user), settings.auth_dev_user
        )
    else:
        user = store.user_for_session(request.cookies.get(SESSION_COOKIE, ""))

    if user is None:
        raise api_error(401, "login_required", "Sign in with GitHub to continue.")
    if user.banned:
        raise api_error(403, "banned", "This account has been suspended.")
    store.touch_user(user.github_id)
    return CurrentUser(
        github_id=user.github_id,
        login=user.login,
        avatar_url=user.avatar_url,
        is_admin=settings.is_admin(user.login),
    )


def same_origin(request: Request) -> None:
    """Refuse state-changing requests sent from another site.

    Defence in depth for the session cookie: SameSite=Lax already keeps it off
    cross-site POSTs, and bodies are JSON (which a plain HTML form cannot send),
    but a browser that sends an ``Origin`` header gets it checked too.
    """
    origin = request.headers.get("origin")
    if origin and origin not in get_settings(request).trusted_origins:
        raise api_error(403, "bad_origin", "Cross-site request refused.")


#: Route parameter type for "requires a signed-in user".
User = Annotated[CurrentUser, Depends(current_user)]


# ------------------------------------------------------------- rate limits

#: bucket -> (per-user rules, per-IP rules, global rules), as Settings fields.
_BUCKETS: dict[str, tuple[str | None, str | None, str | None]] = {
    "query": ("rate_limit_query", "rate_limit_query_ip", None),
    "summarize": ("rate_limit_summarize", "rate_limit_summarize_ip", None),
    "ingest": ("rate_limit_ingest", "rate_limit_ingest_ip", "rate_limit_ingest_global"),
    "add": ("rate_limit_add", "rate_limit_add_ip", None),
    "login": (None, "rate_limit_login_ip", None),
}


@lru_cache(maxsize=64)
def _rules(spec: str) -> tuple[Rule, ...]:
    return parse_rules(spec)


def _humanize(seconds: float) -> str:
    if seconds < 90:
        return f"{max(1, round(seconds))} seconds"
    if seconds < 5_400:
        return f"{round(seconds / 60)} minutes"
    return f"{round(seconds / 3_600)} hours"


def rate_limit_wait(request: Request, bucket: str, user: CurrentUser | None = None) -> float | None:
    """Record a hit in ``bucket``; seconds to wait if over a limit, else None.

    Admins are exempt: they seed demo repositories and debug in production.
    """
    if user is not None and user.is_admin:
        return None
    settings = get_settings(request)
    user_field, ip_field, global_field = _BUCKETS[bucket]
    host = request.client.host if request.client else None
    checks = []
    if user is not None and user_field:
        checks.append((f"{bucket}:user:{user.github_id}", _rules(getattr(settings, user_field))))
    if ip_field:
        ip = client_ip_key(host, request.app.state.ip_secret)
        checks.append((f"{bucket}:ip:{ip}", _rules(getattr(settings, ip_field))))
    if global_field:
        checks.append((f"{bucket}:global", _rules(getattr(settings, global_field))))
    limiter: RateLimiter = request.app.state.limiter
    return limiter.hit(checks)


def enforce_rate_limit(request: Request, bucket: str, user: CurrentUser | None = None) -> None:
    """Raise 429 ``rate_limited`` (with Retry-After) when ``bucket`` is over its limit."""
    wait = rate_limit_wait(request, bucket, user)
    if wait is not None:
        raise api_error(
            429,
            "rate_limited",
            f"Too many requests. Try again in {_humanize(wait)}.",
            headers={"Retry-After": str(math.ceil(wait))},
        )
