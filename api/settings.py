"""Application settings, read once from the environment.

One frozen object instead of ``os.environ`` lookups scattered through the
server, so what the deployment depends on is visible in one place and a bad
production configuration fails at startup rather than on the first request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def _split(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    app_env: str = "development"
    #: Origins allowed by CORS, and by the same-origin check on writes.
    allowed_origins: tuple[str, ...] = _DEV_ORIGINS

    @property
    def production(self) -> bool:
        return self.app_env == "production"

    @classmethod
    def from_env(cls) -> Settings:
        app_env = (os.environ.get("APP_ENV") or "development").strip().lower()
        origins = _split(os.environ.get("ALLOWED_ORIGINS"))
        if not origins:
            if app_env == "production":
                # Same-origin deployments need no CORS at all; never fall back
                # to the dev origins on a public server.
                origins = ()
            else:
                origins = _DEV_ORIGINS
        if "*" in origins:
            raise RuntimeError("ALLOWED_ORIGINS must list origins explicitly, not '*'.")
        return cls(app_env=app_env, allowed_origins=origins)
