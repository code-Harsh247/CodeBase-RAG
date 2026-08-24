# Tasks

**Companion docs:** [PRD.md](./PRD.md) · [ARCHITECTURE.md](./ARCHITECTURE.md)
**Status:** Draft v1
**Last updated:** 2026-08-24

Phases mirror the PRD milestones. Work top to bottom — each phase should leave you with something runnable/demoable, not a half-finished layer.

---

## Phase 0 — Project Setup

- [x] Init repo structure (`ingestion/`, `graph/`, `retrieval/`, `agent/`, `evaluation/`, `api/`, `ui/`, `docs/`)
- [x] `pyproject.toml` with dependencies (tree-sitter, neo4j driver, chromadb, anthropic, fastapi, streamlit)
- [x] `docker-compose.yml`: Neo4j (Community) service + volume
- [x] `.env.example` (Neo4j URI/credentials, Anthropic API key)
- [x] Basic README stub (fill in properly at end of Phase 5)
- [x] GitHub Actions workflow skeleton (lint + test on push, eval run added later)

---

## Phase 1 — Ingestion Pipeline (Python only)

Goal: clone a repo, parse it, and populate Neo4j with a correct graph — verified by hand-written Cypher queries, no agent/LLM involved yet.

- [ ] Repo cloning module (shallow clone to temp dir given a GitHub URL)
- [ ] File walker: collect `.py` files, skip vendored/test/venv dirs by default (configurable)
- [ ] Tree-sitter setup: load Python grammar, parse a file to AST
- [ ] Schema mapper (Python visitor) emitting nodes: `File`, `Module`, `Class`, `Function`, `Method`, `Import`
- [ ] Schema mapper: emit edges `CONTAINS`, `DEFINES`, `IMPORTS`, `INHERITS`
- [ ] `CALLS` edge resolution (name resolution within module scope first; document known limitations for dynamic dispatch)
- [ ] `REFERENCES` edge resolution (type hints, instantiation)
- [ ] Deterministic node ID scheme (`hash(repo + file_path + qualified_name)`)
- [ ] Neo4j bulk loader (batched `UNWIND` writes, not per-node queries)
- [ ] Idempotent re-ingestion (upsert on node ID, no duplicates on re-run)
- [ ] Manual verification: hand-write ~10 Cypher queries against a real small repo and confirm expected results
- [ ] Ingestion CLI command (`ingest <github_url>`) producing a node/edge count summary

**Exit criteria:** can ingest a real small Python repo and get correct answers to structural questions via raw Cypher in Neo4j Browser.

---

## Phase 2 — NL → Cypher Agent (single-shot Q&A)

Goal: ask a question in English, get an answer grounded in one graph query — no multi-hop yet.

- [ ] Schema reference doc for prompting (auto-generated from the schema mapper, not hand-maintained twice)
- [ ] Few-shot example set (question → correct Cypher) covering common patterns (callers, callees, inheritance, imports)
- [ ] `graph_query(cypher)` tool: executes against Neo4j
- [ ] Cypher validation: reject writes/deletes, syntax-check before execution
- [ ] Single LLM call: question + schema + few-shot → Cypher generation
- [ ] Result formatting: Neo4j query results → readable text for the synthesis step
- [ ] Answer synthesis call: results → final answer with `file:line` citations
- [ ] Basic CLI/API endpoint (`query <repo_id> <question>`) wiring it end-to-end
- [ ] Smoke-test against ~10 manually written questions on the Phase 1 test repo

**Exit criteria:** correct answers (with citations) to single-hop structural questions via natural language, end-to-end through the API.

---

## Phase 3 — Hybrid Retrieval + Agentic Multi-Hop

Goal: the agent chooses tools and iterates, matching the "not just RAG" thesis.

- [ ] Function/class summary extraction (signature + docstring, optionally LLM-generated one-liner where docstring is missing)
- [ ] Vector store setup (Chroma), embed summaries keyed by graph node ID
- [ ] `semantic_search(query)` tool: embed query, top-k retrieval, auto-expand each hit into graph neighborhood
- [ ] `grep_and_read(pattern|path)` tool: raw fallback over the local clone
- [ ] Agent loop: tool selection, iteration, stopping condition, max-hop safety limit
- [ ] Tool-call trace logging (surfaced later in UI per transparency NFR)
- [ ] Multi-hop test questions (e.g., "what calls X, and what does that caller do") to validate iteration actually happens
- [ ] Compare single-shot (Phase 2) vs. multi-hop (Phase 3) answers on the same question set — confirm multi-hop wins where expected

**Exit criteria:** agent correctly answers multi-hop and fuzzy/conceptual questions it could not answer in Phase 2, using more than one tool call where needed.

---

## Phase 4 — Second Language (TypeScript/JavaScript)

Goal: prove the schema is actually language-agnostic, not Python-specific.

- [ ] Tree-sitter grammar setup for TypeScript/JS
- [ ] Schema mapper (TS/JS visitor) targeting the same node/edge types as Python
- [ ] `CALLS`/`IMPORTS`/`INHERITS` resolution for TS/JS (ES module imports, class extends, etc.)
- [ ] Ingest a real TS/JS repo, verify via Cypher (same process as Phase 1 exit criteria)
- [ ] Mixed-repo test: ingest a polyglot repo (if available) and confirm no cross-language boundary errors
- [ ] Re-run Phase 2/3 question sets against a TS/JS repo to confirm the agent layer needed zero changes

**Exit criteria:** same query pipeline, zero changes, works correctly against a TypeScript/JS repo.

---

## Phase 5 — Evaluation Harness (the CV headline)

Goal: quantified proof the graph-hybrid approach beats naive RAG.

- [ ] Pick 2-3 real public repos of varying size/language for the eval set
- [ ] Hand-write ~20-30 ground-truth Q&A pairs (mix of structural, multi-hop, and conceptual questions) with correct source locations
- [ ] Build minimal naive-RAG baseline (fixed-size chunk + embed + top-k + LLM answer) — separate, throwaway implementation
- [ ] Retrieval scoring: precision/recall against ground-truth source locations, for both systems
- [ ] Answer correctness scoring (LLM-graded or manual, given small set size)
- [ ] Run both systems across the full question set, capture results
- [ ] Results table + analysis (where graph wins, where it doesn't, why)
- [ ] Wire eval run into GitHub Actions as a regression check
- [ ] Write up results in README (this is the artifact recruiters will actually read)

**Exit criteria:** a checked-in results table with a defensible, honest comparison — including cases where the baseline does fine, if that's what the data shows.

---

## Phase 6 — Stretch (time permitting)

- [ ] Minimal React chat UI replacing Streamlit, showing tool-call trace per PRD transparency NFR
- [ ] Hosted demo: Neo4j AuraDB free tier + backend on a free-tier host
- [ ] Third language support (proves extensibility cheaply once schema is validated)
- [ ] Incremental re-indexing on git diff instead of full re-parse
- [ ] Graph visualization embedded in the UI (beyond Neo4j Browser)
- [ ] Demo GIF/screenshots for README

---

## Notes

- Don't start Phase N+1 before Phase N's exit criteria are met — each phase is deliberately scoped to leave a working, demoable state per [ARCHITECTURE.md](./ARCHITECTURE.md).
- If Phase 1's `CALLS` resolution turns out harder than expected (dynamic dispatch, decorators), that's expected per the PRD risks — document the limitation and move on rather than over-investing before the eval harness can even tell you if it matters.
