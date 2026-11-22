"""FastAPI dependencies: who is asking, and is the request allowed to act."""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request

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
