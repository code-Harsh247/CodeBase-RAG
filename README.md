# CodeGraph

Graph-augmented codebase Q&A for **Python** repositories. Give it a public GitHub repo URL; it parses the code with tree-sitter into a Neo4j knowledge graph (functions, classes, calls, imports, inheritance) and answers natural-language questions using an agent that combines graph queries, semantic search, and grep — instead of naive chunk-and-embed RAG.

**Status:** Phase 3 complete — ingests Python repositories and answers questions by iteratively querying the graph, searching semantically, and reading source, with cited answers. Evaluation against a naive-RAG baseline is Phase 4. See [docs/TASKS.md](docs/TASKS.md).

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

Natural-language querying (Phase 2+) needs a free [Groq](https://console.groq.com) API key in `.env` as `GROQ_API_KEY` — the default LLM provider, chosen over Gemini's free tier (20 requests/day, unworkable) for its much higher quota (1,000/day). See [docs/ARCHITECTURE.md §2.4a](docs/ARCHITECTURE.md#24a-llm-provider) for the reasoning and the reserved paid fallback used only for the Phase 4 eval run.

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

## Development

```bash
ruff check . && pytest
```

Tests that need a live Neo4j skip automatically when none is reachable.

## Docs

- [Product Requirements](docs/PRD.md)
- [Architecture & Design](docs/ARCHITECTURE.md) — including [measured resolution limitations](docs/ARCHITECTURE.md#resolution-what-works-and-what-does-not)
- [Task Breakdown](docs/TASKS.md)
