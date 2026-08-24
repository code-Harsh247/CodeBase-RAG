# CodeGraph

Graph-augmented codebase Q&A for **Python** repositories. Give it a public GitHub repo URL; it parses the code with tree-sitter into a Neo4j knowledge graph (functions, classes, calls, imports, inheritance) and answers natural-language questions using an agent that combines graph queries, semantic search, and grep — instead of naive chunk-and-embed RAG.

**Status:** Phase 1 complete — ingestion pipeline builds the graph for Python repositories. Natural-language querying lands in Phase 2. See [docs/TASKS.md](docs/TASKS.md).

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

## Usage

Ingest a public repository:

```bash
python -m ingestion.cli ingest https://github.com/psf/requests
```

```
repo:    psf/requests @ 8f8b212de8c2
files:   22 parsed, 0 failed
elapsed: 1.9s
nodes:
  Class      53    Function   91    Method   177
  File       22    Import    423    Module    22
edges:
  CALLS      211   CONTAINS  160    DEFINES  163
  IMPORTS    632   INHERITS   37    REFERENCES 131
resolution (rate = resolved / internal references):
  calls      290 resolved, 127 unresolved, 525 out-of-scope  (70%)
```

Query the graph directly:

```bash
python -m ingestion.cli cypher "MATCH (c)-[:CALLS]->(m:Method {qualified_name:'requests.sessions.Session.request'}) RETURN c.qualified_name"
```

Node and edge counts for an ingested repo:

```bash
python -m ingestion.cli stats psf/requests
```

A local directory works anywhere a URL does, which is handy for testing:

```bash
python -m ingestion.cli ingest ./some/local/checkout
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
