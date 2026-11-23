# CodeGraph

**Graph-Augmented Agentic RAG System for Python Codebases.** Give it a public GitHub repo URL; it parses the code with tree-sitter into a Neo4j knowledge graph (functions, classes, calls, imports, inheritance) and answers natural-language questions using an agent that combines graph queries, semantic search, source reading and grep — instead of naive chunk-and-embed RAG. Every answer cites the `file:line` it came from.

**Live demo:** https://codegraph.129-225-109-238.sslip.io — sign in with GitHub; psf/requests and pallets/click are pre-indexed, and you can index your own public repos.

**Results:** measured against a naive-RAG baseline across two structurally different repositories (44 hand-verified questions total), the multi-hop agent answers **86% and 82% correctly**, against 41% and 55% for chunk-and-embed RAG. See [Results](#results) and [docs/EVAL.md](docs/EVAL.md).

**Scope:** Python only, deliberately. The graph schema carries nothing Python-specific and a second language would mean writing one new visitor — but that is a post-MVP extension, not a v1 claim. Depth on one language with measured resolution quality beats a longer language list that is only partly true.

## Why

Naive RAG treats code as text to embed and similarity-search, which loses the structural relationships (call graphs, imports, inheritance) that most real questions about a codebase actually depend on. This project indexes that structure explicitly in a graph database and lets an agent traverse it, falling back to vector/grep search only for fuzzy or conceptual questions.

Full reasoning and design tradeoffs: [docs/PRD.md](docs/PRD.md) · [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## How it works

```
GitHub URL ─▶ hardened clone ─▶ tree-sitter parse ─▶ cross-file resolution ─┬─▶ Neo4j graph
                                                                           └─▶ Chroma vectors (docstring summaries)

question ─▶ agent loop (≤ 6 hops) ─▶ tools: graph_query · semantic_search · read_code · grep ─▶ cited answer, streamed
```

| Layer | What it does | Code |
|---|---|---|
| Ingestion | clone, parse with tree-sitter, resolve calls / bases / imports across files, load graph, embed | `ingestion/` |
| Knowledge stores | Neo4j property graph, Chroma vector index, the source checkout | `graph/`, `retrieval/vector_store.py` |
| Retrieval tools | four tools the LLM can call; model-written Cypher passes a static guard, `EXPLAIN`, and a read-only, time-boxed, row-capped session | `retrieval/` |
| Agent | ReAct-style tool loop with streaming, a single-query baseline, rolling-summary memory budgeted with the real tokenizer | `agent/` |
| API | FastAPI with Server-Sent Events, GitHub sign-in, per-user projects | `api/` |
| Web UI | React + TypeScript chat with a live retrieval trace | `ui/` |
| Evaluation | hand-written benchmark, three systems, mechanical retrieval scoring plus an LLM judge | `evaluation/` |

## Setup

Requires Python 3.11+ and Docker.

```bash
cp .env.example .env          # then set NEO4J_PASSWORD and OPENROUTER_API_KEY
docker compose up -d          # Neo4j, bound to localhost only
python -m venv .venv
source .venv/Scripts/activate # Windows cmd: .venv\Scripts\activate
pip install -e ".[dev]"
```

Neo4j Browser is then at http://localhost:7474, user `neo4j` with the `NEO4J_PASSWORD` you set (there is no default password).

Natural-language querying needs an [OpenRouter](https://openrouter.ai/settings/keys) key (`OPENROUTER_API_KEY`) in `.env`, pinned to `qwen/qwen3-coder` — about **$0.005 a question**, so a few dollars lasts a long time. The first query also downloads that model's tokenizer (a few MB, cached; no model weights) to budget conversation history accurately. Full reasoning, including what else was tested and rejected, in [docs/ARCHITECTURE.md §2.4a](docs/ARCHITECTURE.md#24a-llm-provider).

## Usage

Ingest a public repository:

```bash
python cli.py ingest https://github.com/psf/requests
```

```
repo:    psf/requests @ 8f8b212de8c2
files:   22 parsed, 0 failed
elapsed: 14.8s
nodes:
  Class      53    Function   85    Method   163
  File       22    Import    427    Module    22
edges:
  CALLS      211   CONTAINS  160    DEFINES  163
  IMPORTS    632   INHERITS   37    REFERENCES 131
resolution (rate = resolved / internal references):
  calls      290 resolved, 126 unresolved, 526 out-of-scope  (70%)
embedded: 301 definitions for semantic search
```

Ask a question in English. By default the agent investigates over several hops, choosing between graph queries, semantic search over docstrings, reading source, and grep:

```bash
python cli.py query psf/requests "How does requests handle retries when a request fails?"
```

```
--- hop 1: semantic_search(retry) ---
--- hop 2: grep(Retry) ---
--- hop 3: read_code(requests.adapters.HTTPAdapter.send) ---
--- hop 4: read_code(requests.adapters.HTTPAdapter) ---
--- 5 LLM calls, 15591 tokens (406 reasoning) ---
```

The trace prints to stderr — it is the evidence the answer came from the codebase rather than the model's imagination. Pass `--quiet` for just the answer.

`--single-hop` uses the one-query path instead — cheaper, and enough for purely structural questions:

```bash
python cli.py query --single-hop psf/requests "Which classes inherit from RequestException?"
```

```
--- cypher ---
MATCH (sub:Class {repo_id: $repo_id})-[:INHERITS]->(base:Class)
WHERE base.name = 'RequestException'
RETURN sub.qualified_name AS subclass, sub.file_path AS file, sub.start_line AS line
ORDER BY subclass LIMIT 25
--- 15 rows ---
--- 2 LLM calls, 2732 tokens (163 reasoning) ---
The following classes inherit from `RequestException`:

- `requests.exceptions.HTTPError` (src/requests/exceptions.py:66)
- `requests.exceptions.ConnectionError` (src/requests/exceptions.py:70)
- `requests.exceptions.URLRequired` (src/requests/exceptions.py:102)
… 12 more
```

Query the graph directly with raw Cypher, or get node and edge counts:

```bash
python cli.py cypher "MATCH (c)-[:CALLS]->(m:Method {qualified_name:'requests.sessions.Session.request'}) RETURN c.qualified_name"
python cli.py stats psf/requests
```

From the CLI a local directory works anywhere a URL does, which is handy for testing (`python cli.py ingest ./some/local/checkout`). The web server never indexes local paths.

## Web UI

A React interface that shows the agent investigating, rather than a spinner. Each retrieval step streams in as it happens — which tool ran, the exact Cypher or search query, and the source locations it surfaced — so the answer arrives with its evidence attached.

Two processes, in separate terminals:

```bash
uvicorn api.server:app --port 8000
```

```bash
cd ui && npm install && npm run dev
```

Then open http://localhost:5173. Everything requires a GitHub sign-in. For local development either register a GitHub OAuth App with callback `http://localhost:5173/api/auth/callback` and set `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET`, or set `AUTH_DEV_USER=<your-login>` in `.env` to skip sign-in (refused when `APP_ENV=production`).

Paste a GitHub URL and it indexes, streaming each stage (queue, clone, parse, resolve, build graph, embed). The repository then becomes a conversation you can keep asking follow-up questions in — older turns are folded into a rolling summary, so a long thread keeps its early context. Each user sees the repos they indexed plus public demo repos; history is kept per GitHub account in the browser. Every answer has a **View retrieval trace** button showing the exact queries, searches and files behind it.

## Running it in public

The deployed demo is open to anyone with a GitHub account, so it is built to be safe to leave running:

- **Identity and ownership** — GitHub OAuth (no scopes; the user's token is used once and discarded), server-side sessions stored only as hashes, per-user projects. Removing a repo drops only your claim; the data goes once nobody owns it.
- **Spend cap** — every LLM call reserves its worst-case cost before it runs and settles the real cost afterwards, so concurrent questions cannot overshoot a **$0.25/day** budget (with a per-user share). OpenRouter is held to ceiling prices with `provider.max_price`.
- **Rate limits and concurrency** — sliding-window limits per user and per IP (IPs kept only as an HMAC), at most three questions at once and one per user, one ingest at a time with a short queue.
- **Untrusted repositories** — a GitHub size check before cloning, a hardened shallow clone (no symlinks, timeout, disk cap), file/byte/node caps, symlink and path-containment checks in every tool, a ReDoS-safe `grep`, and eviction of idle repos.
- **Untrusted model output** — model-written Cypher passes a static guard (no writes, no `CALL`, repo-scoped), then `EXPLAIN`, then a READ-mode session with a timeout and row cap. Errors reach the client only as fixed codes.

Deployment is a Docker Compose stack behind Caddy on a single ARM VM; see [deploy/README.md](deploy/README.md) for the first deploy, updates, seeding demo repos and the `codegraph admin` commands (spend, users, bans, repos). Every setting is documented in [.env.example](.env.example).

## Results

22 hand-verified questions each on `psf/requests` (HTTP client, function-call-heavy) and `pallets/click` (CLI framework, decorator- and class-heavy) — chosen to be structurally different, so a result that holds on both is evidence of generalizing rather than a number tuned to one codebase. Three systems scored on identical inputs — same LLM, same embedding model, same grader — so the gap reflects retrieval strategy, not model choice. Full methodology and per-category breakdown: [docs/EVAL.md](docs/EVAL.md).

| system | requests | click |
|---|---|---|
| naive RAG (chunk + embed) | 41% | 55% |
| graph, single Cypher query | 27% | 27% |
| **graph, multi-hop agent** | **86%** | **82%** |

The interesting finding isn't "graph beats vectors" — it's that **iteration is what makes structure useful**. A single Cypher query, run alone, is *worse* than naive RAG on both repos: one wrong guess is fatal with no way to recover, and it scores 0/6 on multi-hop questions almost by definition — those need a name discovered before the real query can even be written. Multi-hop retrieval — query, look at the result, search semantically, read the source, decide the next step — is what turns "the graph didn't have that" into a solved problem. It sweeps or near-sweeps conceptual questions (7/8 and 5/8) where the single-query system manages 0/8 and 1/8, since a question phrased as behaviour has no single query that answers it.

Reproduce it:

```bash
python cli.py eval --questions evaluation/questions/requests.json
python cli.py eval --questions evaluation/questions/click.json
```

## Development

```bash
ruff check . && pytest
cd ui && npx tsc -b && npm run build
```

404 tests, run in CI on every push. Tests that need a live Neo4j skip automatically when none is reachable.

## Docs

- [Product Requirements](docs/PRD.md)
- [Architecture & Design](docs/ARCHITECTURE.md) — including [measured resolution limitations](docs/ARCHITECTURE.md#resolution-what-works-and-what-does-not) and the [eval methodology](docs/ARCHITECTURE.md#26-evaluation-harness)
- [Evaluation: results, methodology, both repositories](docs/EVAL.md)
- [Deployment & operations](deploy/README.md)
- [Task Breakdown & phase-by-phase history](docs/TASKS.md)
