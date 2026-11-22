# CodeGraph: the API, serving the built UI from the same origin.
# Built by deploy/compose.yaml; see deploy/README.md.

# ---- UI -------------------------------------------------------------------
FROM node:22-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

# ---- API ------------------------------------------------------------------
FROM python:3.12-slim

# git: the clone subprocess (ingestion/repo.py). Nothing else is needed:
# every native dependency ships an aarch64 wheel.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY pyproject.toml README.md cli.py ./
COPY agent/ agent/
COPY api/ api/
COPY evaluation/ evaluation/
COPY graph/ graph/
COPY ingestion/ ingestion/
COPY retrieval/ retrieval/
RUN pip install --no-cache-dir . && rm -rf build *.egg-info

COPY --from=ui /ui/dist /app/ui/dist

# Created here so new named volumes inherit the app user's ownership.
#   /data            app.db, clones, the Chroma index
#   /home/app/.cache the embedding model and the Qwen tokenizer (downloaded once)
RUN mkdir -p /data /home/app/.cache && chown -R app:app /data /home/app/.cache

ENV PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    UI_DIST=/app/ui/dist \
    APP_DB_PATH=/data/app.db \
    REPOS_DIR=/data/repos \
    CHROMA_PERSIST_DIR=/data/chroma

USER app
EXPOSE 8000

# One worker, always: rate limits, budget reservations, query slots and the
# ingest line live in-process. --proxy-headers takes the client address from
# Caddy's X-Forwarded-For; trusting any forwarder is safe because this port is
# published nowhere: only containers on the compose networks can reach it,
# and Caddy overwrites any X-Forwarded-For a client sends.
CMD ["uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips=*", \
     "--timeout-graceful-shutdown", "20"]
