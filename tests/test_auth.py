from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from api.settings import Settings

PROFILE = {"id": 4242, "login": "octocat", "avatar_url": "https://avatars.example/1"}


def _github(token_payload=None, profile=PROFILE, seen: list | None = None):
    """A fake GitHub: token exchange plus /user."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json=token_payload or {"access_token": "gho_secret"})
        if request.url.path == "/user":
            assert request.headers["authorization"] == "Bearer gho_secret"
            return httpx.Response(200, json=profile)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _start_login(web) -> str:
    response = web.client.get("/api/auth/login", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://github.com/login/oauth/authorize?")
    return parse_qs(urlsplit(location).query)["state"][0]


def test_login_redirects_with_state_and_no_scopes(web):
    response = web.client.get("/api/auth/login", follow_redirects=False)
    query = parse_qs(urlsplit(response.headers["location"]).query)

    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == ["http://localhost:5173/api/auth/callback"]
    assert len(query["state"][0]) >= 32
    assert "scope" not in query
    cookie = response.headers["set-cookie"]
    assert "cg_oauth_state=" in cookie and "HttpOnly" in cookie


def test_successful_callback_creates_a_session(web):
    seen: list = []
    web.app.state.github_transport = _github(seen=seen)
    state = _start_login(web)

    response = web.client.get(
        f"/api/auth/callback?code=abc&state={state}", follow_redirects=False
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/"
    cookies = response.headers.get_list("set-cookie")
    session = next(c for c in cookies if c.startswith("cg_session="))
    assert "HttpOnly" in session
    assert "samesite=lax" in session.lower()
    assert web.client.get("/api/auth/me").json()["login"] == "octocat"
    # The token exchange sent the secret to GitHub and nowhere else.
    assert {request.url.host for request in seen} == {"github.com", "api.github.com"}


def test_the_github_token_is_never_stored(web, tmp_path):
    web.app.state.github_transport = _github()
    state = _start_login(web)
    web.client.get(f"/api/auth/callback?code=abc&state={state}", follow_redirects=False)

    web.store.close()
    for path in tmp_path.glob("app.db*"):
        assert b"gho_secret" not in path.read_bytes()


@pytest.mark.parametrize("state", ["", "wrong-state"])
def test_callback_with_a_bad_state_is_refused(web, state):
    web.app.state.github_transport = _github()
    _start_login(web)
    response = web.client.get(
        f"/api/auth/callback?code=abc&state={state}", follow_redirects=False
    )
    assert response.headers["location"] == "/?auth_error=state"
    assert web.client.get("/api/auth/me").status_code == 401


def test_callback_without_the_state_cookie_is_refused(web):
    # A login CSRF: the attacker's own callback URL opened in the victim's browser.
    web.app.state.github_transport = _github()
    state = _start_login(web)
    web.client.cookies.clear()
    response = web.client.get(
        f"/api/auth/callback?code=abc&state={state}", follow_redirects=False
    )
    assert response.headers["location"] == "/?auth_error=state"


def test_denied_consent_does_not_sign_in(web):
    web.app.state.github_transport = _github(token_payload={"error": "access_denied"})
    state = _start_login(web)
    response = web.client.get(
        f"/api/auth/callback?code=abc&state={state}", follow_redirects=False
    )
    assert response.headers["location"] == "/?auth_error=denied"


def test_github_outage_is_a_clean_failure(web):
    web.app.state.github_transport = httpx.MockTransport(lambda request: httpx.Response(502))
    state = _start_login(web)
    response = web.client.get(
        f"/api/auth/callback?code=abc&state={state}", follow_redirects=False
    )
    assert response.headers["location"] == "/?auth_error=github"


def test_banned_users_get_no_session(web):
    web.store.upsert_user(PROFILE["id"], PROFILE["login"])
    web.store.set_banned(PROFILE["login"], True)
    web.app.state.github_transport = _github()
    state = _start_login(web)

    response = web.client.get(
        f"/api/auth/callback?code=abc&state={state}", follow_redirects=False
    )
    assert response.headers["location"] == "/?auth_error=banned"
    assert not any(c.startswith("cg_session=") for c in response.headers.get_list("set-cookie"))


def test_banning_a_signed_in_user_locks_them_out(web):
    web.login_as("mallory")
    assert web.client.get("/api/auth/me").status_code == 200
    web.store.set_banned("mallory", True)
    assert web.client.get("/api/auth/me").status_code == 401


def test_me_reports_admin(web):
    web.login_as("admin")
    assert web.client.get("/api/auth/me").json()["is_admin"] is True
    web.login_as("someone")
    assert web.client.get("/api/auth/me").json()["is_admin"] is False


def test_logout_invalidates_the_session(web):
    web.login_as("alice")
    token = web.client.cookies.get("cg_session")
    assert web.client.post("/api/auth/logout").status_code == 200
    # Even replaying the old cookie no longer works.
    web.client.cookies.set("cg_session", token)
    assert web.client.get("/api/auth/me").status_code == 401


def test_expired_sessions_are_rejected(web):
    user = web.store.upsert_user(7, "late")
    web.client.cookies.set("cg_session", web.store.create_session(user.github_id, ttl_days=0))
    assert web.client.get("/api/auth/me").status_code == 401


def test_dev_user_is_refused_in_production():
    with pytest.raises(RuntimeError, match="AUTH_DEV_USER"):
        Settings(
            app_env="production",
            auth_dev_user="me",
            github_client_id="x",
            github_client_secret="y",
            public_base_url="https://demo.example",
        ).check()


def test_production_requires_oauth_and_https():
    with pytest.raises(RuntimeError) as caught:
        Settings(app_env="production", public_base_url="http://demo.example").check()
    assert "GITHUB_CLIENT_ID" in str(caught.value)
    assert "https" in str(caught.value)


def test_dev_user_bypass_works_outside_production(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from api import server

    monkeypatch.setattr(server, "_client", lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    app = server.create_app(Settings(app_db_path=tmp_path / "a.db", auth_dev_user="devuser"))
    with TestClient(app) as client:
        assert client.get("/api/auth/me").json()["login"] == "devuser"
