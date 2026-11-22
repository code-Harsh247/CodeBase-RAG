# Deploying CodeGraph

CodeGraph runs as its own Docker Compose stack (`name: codegraph`) behind the
VM's shared Caddy proxy:

```
internet ─▶ Caddy (~/proxy, :443) ─▶ codegraph:8000 (API + built UI) ─▶ neo4j:7687
                                      network "web"                    private network
```

Nothing in this stack publishes a port. State lives in three named volumes:
`data` (app.db, clones, Chroma index), `cache` (embedding model, tokenizer)
and `neo4j_data`.

## First deploy

1. **Secrets.** Fill in `.env.production` at the repo root (git ignores it).
   `PUBLIC_BASE_URL` must be `https://<site>`, and the production GitHub OAuth
   app's callback must be `https://<site>/api/auth/callback`.
2. **Code and secrets on the VM:**
   ```bash
   ssh ubuntu@<vm> git clone https://github.com/code-Harsh247/CodeBase-RAG.git apps/codegraph
   scp .env.production ubuntu@<vm>:apps/codegraph/deploy/.env
   ```
3. **Build and start:**
   ```bash
   ssh ubuntu@<vm> bash apps/codegraph/deploy/setup.sh <site>
   ```
   The first build takes a few minutes; later ones reuse cached layers.
4. **Seed the public demo repositories** (visible to every user; only admins
   can remove them):
   ```bash
   cd ~/apps/codegraph/deploy
   sudo docker compose exec api codegraph ingest https://github.com/psf/requests --public
   sudo docker compose exec api codegraph ingest https://github.com/pallets/click --public
   ```

## Updating

Push to `main`, then on the VM: `bash ~/apps/codegraph/deploy/setup.sh <site>`.
It pulls, rebuilds, waits for the health check and reloads Caddy.

## Operating

From `~/apps/codegraph/deploy`:

| Task | Command |
|---|---|
| Logs | `sudo docker compose logs -f api` |
| Today's spend, top users | `sudo docker compose exec api codegraph admin spend` |
| Repos / users | `... codegraph admin repos` / `... codegraph admin users` |
| Ban someone (ends their sessions) | `... codegraph admin ban <login>` |
| Delete a repo for everyone | `... codegraph admin delete <owner/name>` |
| Make a repo public | `... codegraph admin public <owner/name>` |

Limits, the daily budget and everything else are set in `deploy/.env`; see
`.env.example` for the full list. Restart after changing them:
`sudo docker compose up -d`.
