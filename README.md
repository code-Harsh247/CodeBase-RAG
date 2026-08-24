# CodeGraph

Graph-augmented codebase Q&A for **Python** repositories. Give it a public GitHub repo URL; it parses the code with tree-sitter into a Neo4j knowledge graph (functions, classes, calls, imports, inheritance) and answers natural-language questions using an agent that combines graph queries, semantic search, and grep — instead of naive chunk-and-embed RAG.

**Status:** Phase 4 complete — measured against a naive-RAG baseline on 22 hand-verified questions, the multi-hop agent answers **95% correctly with zero wrong answers**, against 64% for chunk-and-embed RAG. See [Results](#results) below and [docs/TASKS.md](docs/TASKS.md).

**Scope:** Python only, deliberately. The graph schema carries nothing Python-specific and a second language would mean writing one new visitor — but that is a post-MVP extension, not a v1 claim. Depth on one language with measured resolution quality beats a longer language list that is only partly true.

## Why

Naive RAG treats code as text to embed and similarity-search, which loses the structural relationships (call graphs, imports, inheritance) that most real questions about a codebase actually depend on. This project indexes that structure explicitly in a graph database and lets an agent traverse it, falling back to vector/grep search only for fuzzy or conceptual questions.

Full reasoning and design tradeoffs: [docs/PRD.md](docs/PRD.md) · [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Setup

Requires Python 3.11+ and Docker.

```bash
cp .env.example .env
docker compose up -d
python -m venv .venv && source .venv/Scripts/activate
pip install -e ".[dev]"
```

Neo4j Browser is then at http://localhost:7474 (default credentials `neo4j` / `changeme123`).

Natural-language querying (Phase 2+) needs a free [Groq](https://console.groq.com) API key in `.env` as `GROQ_API_KEY` — the default LLM provider, chosen over Gemini's free tier (20 requests/day, unworkable) for its much higher quota (1,000/day). The Phase 4 scored eval run instead uses a pinned model on [OpenRouter](https://openrouter.ai/settings/keys) (`OPENROUTER_API_KEY`, `--provider openrouter`) — full reasoning, including what else was tested and rejected along the way, in [docs/ARCHITECTURE.md §2.4a](docs/ARCHITECTURE.md#24a-llm-provider).

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

Ask a question in English:

```bash
python cli.py query psf/requests "Which classes inherit from RequestException?"
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

By default the agent investigates over several hops, choosing between graph
queries, semantic search over docstrings, reading source, and grep:

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

The trace prints to stderr — it is the evidence the answer came from the
codebase rather than the model's imagination. Pass `--quiet` for just the
answer, or `--single-hop` to use the one-query path instead (cheaper, and
enough for purely structural questions).

Query the graph directly with raw Cypher:

```bash
python cli.py cypher "MATCH (c)-[:CALLS]->(m:Method {qualified_name:'requests.sessions.Session.request'}) RETURN c.qualified_name"
```

Node and edge counts for an ingested repo:

```bash
python cli.py stats psf/requests
```

A local directory works anywhere a URL does, which is handy for testing:

```bash
python cli.py ingest ./some/local/checkout
```

## Web UI

A React interface that shows the agent investigating, rather than a spinner. Each retrieval step streams in as it happens — which tool ran, the exact Cypher or search query, and the source locations it surfaced — so the answer arrives with its evidence attached.

Two processes, in separate terminals:

```bash
uvicorn api.server:app --port 8000
```

```bash
cd ui && npm install && npm run dev
```

Then open http://localhost:5173, paste a GitHub URL, and ask about it — indexing streams its progress (clone, parse, resolve, build graph, embed) rather than sitting behind a spinner, and the repository is selected automatically when it finishes. The retrieval mode toggle switches between the multi-hop agent and the single-query path, which makes the difference measured below visible directly.

Ingesting from the UI does the same work as `cli.py ingest`, so either entry point is fine.

## Results

22 hand-verified questions over `psf/requests`, three systems scored on identical inputs — same LLM, same embedding model, same grader — so the gap reflects retrieval strategy, not model choice. Full methodology: [docs/ARCHITECTURE.md §2.6](docs/ARCHITECTURE.md).

| system | accuracy | wrong | tokens/question |
|---|---|---|---|
| naive RAG (chunk + embed) | 64% | 2 | ~3,350 |
| graph, single Cypher query | 36% | 10 | ~2,470 |
| **graph, multi-hop agent** | **95%** | **0** | ~11,540 |

The interesting finding isn't "graph beats vectors" — it's that **iteration is what makes structure useful**. A single Cypher query, run alone, is *worse* than naive RAG: one wrong guess is fatal with no way to recover, and it scores 0/6 on questions that need a name discovered before the real query can even be written. Multi-hop retrieval — query, look at the result, search semantically, read the source, decide the next step — is what turns "the graph didn't have that" into a solved problem, at a real cost: ~3.4x the baseline's tokens.

Reproduce it:

```bash
python cli.py eval --provider openrouter --model qwen/qwen3-coder
```

## Development

```bash
ruff check . && pytest
```

Tests that need a live Neo4j skip automatically when none is reachable.

## Docs

- [Product Requirements](docs/PRD.md)
- [Architecture & Design](docs/ARCHITECTURE.md) — including [measured resolution limitations](docs/ARCHITECTURE.md#resolution-what-works-and-what-does-not) and the [eval methodology](docs/ARCHITECTURE.md#26-evaluation-harness)
- [Task Breakdown & full Phase 4 results](docs/TASKS.md)
