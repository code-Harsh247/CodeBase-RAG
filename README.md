# CodeGraph

Graph-augmented codebase Q&A. Give it a public GitHub repo URL; it parses the code with tree-sitter into a Neo4j knowledge graph (functions, classes, calls, imports, inheritance) and answers natural-language questions using an agent that combines graph queries, semantic search, and grep — instead of naive chunk-and-embed RAG.

**Status:** early development (Phase 0 — project scaffolding). See [docs/TASKS.md](docs/TASKS.md) for progress.

## Why

Naive RAG treats code as text to embed and similarity-search, which loses the structural relationships (call graphs, imports, inheritance) that most real questions about a codebase actually depend on. This project indexes that structure explicitly in a graph database and lets an agent traverse it, falling back to vector/grep search only for fuzzy or conceptual questions.

Full reasoning and design tradeoffs: [docs/PRD.md](docs/PRD.md) · [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Setup

Full setup instructions land at the end of Phase 1. For now:

```bash
cp .env.example .env
docker-compose up -d
```

## Docs

- [Product Requirements](docs/PRD.md)
- [Architecture & Design](docs/ARCHITECTURE.md)
- [Task Breakdown](docs/TASKS.md)
