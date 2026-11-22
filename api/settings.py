"""Application settings, read once from the environment.

One frozen object instead of ``os.environ`` lookups scattered through the
server, so what the deployment depends on is visible in one place and a bad
production configuration fails at startup rather than on the first request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

_DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")
_DEV_BASE_URL = "http://localhost:5173"


def _split(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


@dataclass(frozen=True)
class Settings:
    app_env: str = "development"
    #: Origins allowed by CORS, and by the same-origin check on writes.
    allowed_origins: tuple[str, ...] = _DEV_ORIGINS
    #: Where the browser reaches the app; builds the OAuth callback URL.
    public_base_url: str = _DEV_BASE_URL
    app_db_path: Path = Path(".data/app.db")

    github_client_id: str = ""
    github_client_secret: str = field(default="", repr=False)
    #: Lower-cased GitHub logins with admin rights (delete anything, refresh).
    admin_logins: frozenset[str] = frozenset()
    session_ttl_days: int = 30
    #: Development only: act as this GitHub login without signing in.
    auth_dev_user: str = ""
    #: Repositories every signed-in user can see; admins seed these.
    public_repos: tuple[str, ...] = ()

    @property
    def production(self) -> bool:
        return self.app_env == "production"

    @property
    def secure_cookies(self) -> bool:
        return self.public_base_url.startswith("https://")

    @property
    def oauth_configured(self) -> bool:
        return bool(self.github_client_id and self.github_client_secret)

    @property
    def trusted_origins(self) -> frozenset[str]:
        """Origins a state-changing request may come from."""
        return frozenset({*self.allowed_origins, origin_of(self.public_base_url)})

    def is_admin(self, login: str) -> bool:
        return login.lower() in self.admin_logins

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        app_env = (env("APP_ENV") or "development").strip().lower()
        production = app_env == "production"

        origins = _split(env("ALLOWED_ORIGINS"))
        if not origins and not production:
            # Production is same-origin behind the proxy and needs no CORS;
            # never fall back to the dev origins on a public server.
            origins = _DEV_ORIGINS
        if "*" in origins:
            raise RuntimeError("ALLOWED_ORIGINS must list origins explicitly, not '*'.")

        settings = cls(
            app_env=app_env,
            allowed_origins=origins,
            public_base_url=(env("PUBLIC_BASE_URL") or ("" if production else _DEV_BASE_URL))
            .strip()
            .rstrip("/"),
            app_db_path=Path(env("APP_DB_PATH") or ".data/app.db"),
            github_client_id=(env("GITHUB_CLIENT_ID") or "").strip(),
            github_client_secret=(env("GITHUB_CLIENT_SECRET") or "").strip(),
            admin_logins=frozenset(login.lower() for login in _split(env("ADMIN_GITHUB_LOGINS"))),
            session_ttl_days=int(env("SESSION_TTL_DAYS") or 30),
            auth_dev_user=(env("AUTH_DEV_USER") or "").strip(),
            public_repos=_split(env("PUBLIC_REPOS")),
        )
        settings.check()
        return settings

    def check(self) -> None:
        """Refuse to start with a configuration that would be unsafe in public."""
        if not self.production:
            return
        problems = []
        if self.auth_dev_user:
            problems.append("AUTH_DEV_USER must not be set in production")
        if not self.oauth_configured:
            problems.append("GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET are required")
        if not self.public_base_url.startswith("https://"):
            problems.append("PUBLIC_BASE_URL must be an https:// URL")
        if problems:
            raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))
