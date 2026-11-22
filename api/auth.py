"""Sign in with GitHub (OAuth web flow), with server-side sessions.

What is kept, and what is not:

- No scopes are requested. The public profile (id, login, avatar) is all the
  demo needs, so the consent screen asks for nothing.
- The user's GitHub access token is used once, to read that profile, and then
  dropped. It is never written anywhere.
- The session is a random token in an HttpOnly cookie. The server stores only
  its SHA-256 hash, so the database alone cannot be used to sign in.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from api.deps import (
    SESSION_COOKIE,
    User,
    api_error,
    get_settings,
    get_store,
    same_origin,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth")

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"

STATE_COOKIE = "cg_oauth_state"
_STATE_TTL = 600


def _redirect_uri(request: Request) -> str:
    return f"{get_settings(request).public_base_url}/api/auth/callback"


def _http(request: Request) -> httpx.Client:
    # Tests install an httpx.MockTransport here; production uses the network.
    transport = getattr(request.app.state, "github_transport", None)
    return httpx.Client(transport=transport, timeout=10)


def _fail(code: str) -> RedirectResponse:
    response = RedirectResponse(f"/?auth_error={code}", status_code=302)
    response.delete_cookie(STATE_COOKIE, path="/api/auth")
    return response


@router.get("/login")
def login(request: Request) -> Response:
    settings = get_settings(request)
    if settings.auth_dev_user and not settings.production:
        return RedirectResponse("/", status_code=302)
    if not settings.oauth_configured:
        raise api_error(503, "auth_unavailable", "Sign-in is not configured on this server.")

    state = secrets.token_urlsafe(32)
    query = urlencode(
        {
            "client_id": settings.github_client_id,
            "redirect_uri": _redirect_uri(request),
            "state": state,
            "allow_signup": "true",
        }
    )
    response = RedirectResponse(f"{AUTHORIZE_URL}?{query}", status_code=302)
    # Binds the callback to the browser that started the flow (login CSRF).
    response.set_cookie(
        STATE_COOKIE,
        state,
        max_age=_STATE_TTL,
        path="/api/auth",
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
    )
    return response


@router.get("/callback")
def callback(request: Request, code: str = "", state: str = "") -> Response:
    settings = get_settings(request)
    store = get_store(request)
    if not settings.oauth_configured:
        return _fail("unavailable")

    expected = request.cookies.get(STATE_COOKIE, "")
    if not code or not state or not expected or not hmac.compare_digest(state, expected):
        return _fail("state")

    try:
        with _http(request) as http:
            token_response = http.post(
                TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": settings.github_client_id,
                    "client_secret": settings.github_client_secret,
                    "code": code,
                    "redirect_uri": _redirect_uri(request),
                },
            )
            token_response.raise_for_status()
            access_token = token_response.json().get("access_token")
            if not access_token:
                return _fail("denied")

            profile_response = http.get(
                USER_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            profile_response.raise_for_status()
            profile = profile_response.json()
        # The token goes out of scope here and is never stored.
        del access_token
        github_id = int(profile["id"])
        login_name = str(profile["login"])
        avatar_url = str(profile.get("avatar_url") or "")
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        logger.warning("GitHub sign-in failed", exc_info=True)
        return _fail("github")

    user = store.upsert_user(github_id, login_name, avatar_url)
    if user.banned:
        return _fail("banned")

    token = store.create_session(user.github_id, settings.session_ttl_days)
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(STATE_COOKIE, path="/api/auth")
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_days * 86_400,
        path="/",
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
    )
    logger.info("signed in: %s", login_name)
    return response


@router.get("/me")
def me(user: User) -> dict:
    return {"login": user.login, "avatar_url": user.avatar_url, "is_admin": user.is_admin}


@router.post("/logout", dependencies=[Depends(same_origin)])
def logout(request: Request) -> Response:
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        get_store(request).delete_session(token)
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
