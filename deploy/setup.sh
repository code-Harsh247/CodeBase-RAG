#!/usr/bin/env bash
# Sets up (or updates) CodeGraph on a VM that hosts several projects behind
# one shared Caddy proxy (~/proxy, ports 80/443, on the Docker network "web").
#
# Usage:  bash setup.sh <site address>
#   e.g.  bash setup.sh codegraph.129-225-109-238.sslip.io
#
# Expects ~/apps/codegraph/deploy/.env to exist (copied from .env.production;
# see deploy/README.md). Safe to run again: it pulls, rebuilds and restarts.
set -euo pipefail

SITE_ADDRESS="${1:?usage: setup.sh <site address, e.g. codegraph.129-225-109-238.sslip.io>}"
REPO="https://github.com/code-Harsh247/CodeBase-RAG.git"
APP_DIR="$HOME/apps/codegraph"
PROXY_DIR="$HOME/proxy"

echo "== 1. Code"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO" "$APP_DIR"
fi

echo "== 2. Secrets"
ENV_FILE="$APP_DIR/deploy/.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "missing $ENV_FILE: copy .env.production there first (see deploy/README.md)" >&2
  exit 1
fi
chmod 600 "$ENV_FILE"
expected="https://$SITE_ADDRESS"
actual=$(grep -E '^PUBLIC_BASE_URL=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")
if [ "$actual" != "$expected" ]; then
  echo "PUBLIC_BASE_URL in $ENV_FILE is '$actual', expected '$expected'" >&2
  echo "(it must also match the GitHub OAuth app's callback URL)" >&2
  exit 1
fi

echo "== 3. Shared network and proxy"
sudo docker network inspect web >/dev/null 2>&1 || sudo docker network create web
if [ ! -f "$PROXY_DIR/compose.yaml" ]; then
  echo "no shared proxy at $PROXY_DIR; set one up first (see deploy/README.md)" >&2
  exit 1
fi

echo "== 4. Build and start"
cd "$APP_DIR/deploy"
sudo docker compose up -d --build --remove-orphans
sudo docker image prune -f >/dev/null

echo "== 5. Wait for the API"
for _ in $(seq 1 60); do
  status=$(sudo docker inspect -f '{{.State.Health.Status}}' "$(sudo docker compose ps -q api)" 2>/dev/null || true)
  [ "$status" = "healthy" ] && break
  sleep 5
done
echo "api: ${status:-unknown}"
[ "$status" = "healthy" ] || { sudo docker compose logs --tail 50 api; exit 1; }

echo "== 6. Site"
sed "s/SITE_ADDRESS/$SITE_ADDRESS/" "$APP_DIR/deploy/codegraph.caddy" > "$PROXY_DIR/sites/codegraph.caddy"
sudo docker compose -f "$PROXY_DIR/compose.yaml" exec -T caddy caddy reload --config /etc/caddy/Caddyfile

echo
echo "Deployed: https://$SITE_ADDRESS"
