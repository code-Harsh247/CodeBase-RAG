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

from agent.metering import prices_from_env

_DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")
_DEV_BASE_URL = "http://localhost:5173"


def _split(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def _optional_int(value: str | None, default: int | None) -> int | None:
    """Unset -> ``default``; ``0`` or ``off`` -> None (disabled)."""
    if value is None or not value.strip():
        return default
    if value.strip().lower() in {"0", "off", "none"}:
        return None
    return int(value)


def _optional_float(value: str | None, default: float | None) -> float | None:
    if value is None or not value.strip():
        return default
    if value.strip().lower() in {"0", "off", "none"}:
        return None
    return float(value)


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

    # -- ingestion limits (see ingestion/limits.py) ------------------------
    #: App-level GitHub token for the size pre-check; needs no scopes.
    github_token: str = field(default="", repr=False)
    max_repo_size_mb: int = 200
    max_clone_disk_mb: int = 300
    clone_timeout_s: float = 90
    ingest_deadline_s: float = 300
    max_py_files: int = 2_000
    max_py_total_mb: int = 30
    max_graph_nodes: int = 60_000
    min_free_disk_gb: float = 5
    max_repos_per_user: int = 5
    #: Waiting ingestions allowed behind the one running.
    ingest_queue_max: int = 3
    #: Eviction. ``None`` turns it off, which is the default outside
    #: production so a developer's local repositories are never deleted.
    max_indexed_repos: int | None = None
    repo_idle_ttl_days: float | None = None

    # -- spend (see agent/metering.py) ------------------------------------
    daily_budget_usd: float = 0.25
    #: One account's share of the daily budget.
    per_user_daily_usd: float = 0.06
    #: Ceiling prices, USD per million tokens; OpenRouter is held to them.
    llm_price_input_per_m: float = 0.40
    llm_price_output_per_m: float = 1.60
    #: A question still running after this long is stopped.
    query_deadline_s: float = 180

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
            github_token=(env("GITHUB_TOKEN") or "").strip(),
            max_repo_size_mb=int(env("MAX_REPO_SIZE_MB") or 200),
            max_clone_disk_mb=int(env("MAX_CLONE_DISK_MB") or 300),
            clone_timeout_s=float(env("CLONE_TIMEOUT_SECONDS") or 90),
            ingest_deadline_s=float(env("INGEST_DEADLINE_SECONDS") or 300),
            max_py_files=int(env("MAX_PY_FILES") or 2_000),
            max_py_total_mb=int(env("MAX_PY_TOTAL_MB") or 30),
            max_graph_nodes=int(env("MAX_GRAPH_NODES") or 60_000),
            min_free_disk_gb=float(env("MIN_FREE_DISK_GB") or 5),
            max_repos_per_user=int(env("MAX_REPOS_PER_USER") or 5),
            ingest_queue_max=int(env("INGEST_QUEUE_MAX") or 3),
            max_indexed_repos=_optional_int(env("MAX_INDEXED_REPOS"), 50 if production else None),
            repo_idle_ttl_days=_optional_float(
                env("REPO_IDLE_TTL_DAYS"), 7 if production else None
            ),
            daily_budget_usd=float(env("DAILY_BUDGET_USD") or 0.25),
            per_user_daily_usd=float(env("PER_USER_DAILY_USD") or 0.06),
            llm_price_input_per_m=prices_from_env()[0],
            llm_price_output_per_m=prices_from_env()[1],
            query_deadline_s=float(env("QUERY_DEADLINE_SECONDS") or 180),
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
