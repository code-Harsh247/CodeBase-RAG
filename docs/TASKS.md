# Tasks

**Companion docs:** [PRD.md](./PRD.md) · [ARCHITECTURE.md](./ARCHITECTURE.md)
**Status:** Draft v2 — scoped to Python only
**Last updated:** 2026-08-24

Phases mirror the PRD milestones. Work top to bottom — each phase should leave you with something runnable/demoable, not a half-finished layer.

---

## Phase 0 — Project Setup

- [x] Init repo structure (`ingestion/`, `graph/`, `retrieval/`, `agent/`, `evaluation/`, `api/`, `ui/`, `docs/`)
- [x] `pyproject.toml` with dependencies (tree-sitter, neo4j driver, chromadb, LLM provider SDK, fastapi, streamlit)
- [x] `docker-compose.yml`: Neo4j (Community) service + volume
- [x] `.env.example` (Neo4j URI/credentials, LLM provider API key)
- [x] Basic README stub (fill in properly at end of Phase 5)
- [x] GitHub Actions workflow skeleton (lint + test on push, eval run added later)

---

## Phase 1 — Ingestion Pipeline (Python only)

Goal: clone a repo, parse it, and populate Neo4j with a correct graph — verified by hand-written Cypher queries, no agent/LLM involved yet.

- [x] Repo cloning module (shallow clone to temp dir given a GitHub URL)
- [x] File walker: collect `.py` files, skip vendored/test/venv dirs by default (configurable)
- [x] Tree-sitter setup: load Python grammar, parse a file to AST
- [x] Schema mapper (Python visitor) emitting nodes: `File`, `Module`, `Class`, `Function`, `Method`, `Import`
- [x] Schema mapper: emit edges `CONTAINS`, `DEFINES`, `IMPORTS`, `INHERITS`
- [x] `CALLS` edge resolution (name resolution within module scope first; document known limitations for dynamic dispatch)
- [x] `REFERENCES` edge resolution (type hints, instantiation)
- [x] Deterministic node ID scheme (`hash(repo + file_path + qualified_name)`)
- [x] Neo4j bulk loader (batched `UNWIND` writes, not per-node queries)
- [x] Idempotent re-ingestion (upsert on node ID, no duplicates on re-run)
- [x] Manual verification: hand-write ~10 Cypher queries against a real small repo and confirm expected results
- [x] Ingestion CLI command (`ingest <github_url>`) producing a node/edge count summary

**Exit criteria:** can ingest a real small Python repo and get correct answers to structural questions via raw Cypher in Neo4j Browser. ✅

Verified against `psf/requests` (22 files, ~2s) and `pallets/click` (32 files, ~3.5s):
call resolution 70% / 83% of internal call sites, inheritance and import queries
correct on spot-checks against source. Known limitations are documented in
[ARCHITECTURE.md](./ARCHITECTURE.md#resolution-what-works-and-what-does-not).

---

## Phase 2 — NL → Cypher Agent (single-shot Q&A)

Goal: ask a question in English, get an answer grounded in one graph query — no multi-hop yet.

- [x] `LLMProvider` interface (`generate`, plus a structured-output variant) — see ARCHITECTURE.md §2.4a
- [x] Groq provider implementation (default: GPT-OSS 120B), reading `GROQ_API_KEY` from env
- [x] Schema reference doc for prompting (auto-generated from the schema mapper, not hand-maintained twice)
- [x] Few-shot example set (question → correct Cypher) covering common patterns (callers, callees, inheritance, imports)
- [x] `graph_query(cypher)` tool: executes against Neo4j
- [x] Cypher validation: reject writes/deletes, syntax-check before execution
- [x] Single LLM call: question + schema + few-shot → Cypher generation
- [x] Retry loop: feed validation/database errors back to the model (bounded attempts)
- [x] Result formatting: Neo4j query results → readable text for the synthesis step
- [x] Answer synthesis call: results → final answer with `file:line` citations
- [x] Basic CLI/API endpoint (`query <repo_id> <question>`) wiring it end-to-end
- [x] Smoke-test against ~10 manually written questions on the Phase 1 test repo

**Exit criteria:** correct answers (with citations) to single-hop structural questions via natural language, end-to-end through the API. ✅

Smoke test on `psf/requests`, 10 questions: **10/10 produced valid, executable
Cypher and non-empty results**, 2 needed one retry each (the error-feedback loop
recovered both). ~2,100-4,500 tokens per question across 2-3 LLM calls.

Safety verified against the live database: write clauses (`DETACH DELETE`,
`SET`, `DROP`) and unscoped queries are all rejected before execution, node
count unchanged. A question asking the agent to delete data is refused rather
than answered.

---

## Phase 3 — Hybrid Retrieval + Agentic Multi-Hop

Goal: the agent chooses tools and iterates, matching the "not just RAG" thesis.

- [x] Docstring extraction added to the mapper (it captured none before Phase 3)
- [x] Function/class summary extraction (qualified name + signature + docstring)
- [x] Vector store setup (Chroma, ONNX MiniLM embeddings — no torch), keyed by graph node ID
- [x] `semantic_search(query)` tool: embed query, top-k retrieval, auto-expand each hit into graph neighborhood
- [x] `read_code(name|path)` and `grep(pattern)` tools over the local clone
- [x] Agent loop: native tool calling, iteration, stopping condition, max-hop safety limit
- [x] Tool-call trace logging (printed to stderr per transparency NFR)
- [x] Multi-hop test questions to validate iteration actually happens
- [~] Compare single-shot (Phase 2) vs. multi-hop (Phase 3) — **partial, see below**

**Exit criteria:** agent correctly answers multi-hop and fuzzy/conceptual questions it could not answer in Phase 2, using more than one tool call where needed. ✅ (demonstrated; comparison incomplete)

Multi-hop retrieval works and chains tools as designed — e.g. "how does requests
handle retries" runs `semantic_search -> grep -> read_code -> read_code` and
produces a correctly cited answer that no single Cypher query could reach.

The clearest win so far is "Where is SSL certificate verification handled?":
single-shot answered `HTTPAdapter.send` (the caller), multi-hop found
`HTTPAdapter.cert_verify` at src/requests/adapters.py:307 — the method that
actually does it.

**The comparison is unfinished.** Groq enforces an undocumented 200,000
tokens/day cap that we hit partway through; two behavioural questions have no
multi-hop result. What the partial run showed:

| kind | single-shot | multi-hop | tokens (single vs multi) |
|------|-------------|-----------|--------------------------|
| structural (2) | 2/2 | 2/2 | 5,481 vs 11,129 |
| behavioural (3) | 3/3 by row count | 1/3 completed | 8,573 vs 10,763 |

Two caveats worth carrying into Phase 4, both about measurement rather than the
system: the "single-shot 3/3" is scored on *returning rows*, which is not the
same as answering well — one of those answers was the hedge "it calls another
function whose name contains 'cookie'". And multi-hop costs 2-4x the tokens for
structural questions it has no advantage on. Deciding when the extra cost is
warranted needs the graded eval, not row counts.

---

## Phase 4 — Evaluation Harness (the CV headline)

Goal: quantified proof the graph-hybrid approach beats naive RAG.

- [x] Eval repo chosen: `psf/requests` (`src/` layout). A second repo is deferred — see the note below on what one run cost in time and money.
- [x] 22 ground-truth Q&A pairs written from the source (8 structural, 6 multi-hop, 8 conceptual) — `evaluation/questions/requests.json`
- [x] Naive-RAG baseline: fixed 40-line chunks with overlap, same embedding model and same LLM as the graph systems — `evaluation/baseline.py`
- [x] Retrieval scoring: recall/precision against ground-truth locations, taken from structured fields rather than parsed from rendered text
- [x] Answer correctness: LLM-graded against the hand-written reference, same grader and rubric for every system
- [x] Provider decision made and implemented: develop the harness on Groq (free, but capped at 200,000 tokens/day — won't fit a full sweep); run the scored eval on `--provider openrouter --model qwen/qwen3-coder`, verified against real questions and under $0.25 for the whole sweep. `agent/openrouter_provider.py`. See ARCHITECTURE.md §2.4a for what else was tested (Gemini, `openrouter/free`, two local Ollama models) and rejected.
- [x] Per-question token cost recorded alongside accuracy — the cost gap turned out to be the main caveat on the headline result
- [x] Full run over all three systems, results in `evaluation/results.json`
- [ ] Results table + analysis (where graph wins, where it doesn't, why)
- [ ] Wire eval run into GitHub Actions as a regression check
- [ ] Write up results in README (this is the artifact recruiters will actually read)

**Exit criteria:** a checked-in results table with a defensible, honest comparison — including cases where the baseline does fine, if that's what the data shows.

---

## Phase 5 — Stretch (time permitting)

- [ ] Minimal React chat UI replacing Streamlit, showing tool-call trace per PRD transparency NFR
- [ ] Hosted demo: Neo4j AuraDB free tier + backend on a free-tier host
- [ ] Return-value type inference (the largest known resolution gap — see ARCHITECTURE.md)
- [ ] A second language, if and only if the Python path is fully done and measured — this is the one place the "language-agnostic schema" claim gets proven rather than argued
- [ ] Incremental re-indexing on git diff instead of full re-parse
- [ ] Graph visualization embedded in the UI (beyond Neo4j Browser)
- [ ] Demo GIF/screenshots for README

---

## Notes

- Don't start Phase N+1 before Phase N's exit criteria are met — each phase is deliberately scoped to leave a working, demoable state per [ARCHITECTURE.md](./ARCHITECTURE.md).
- If Phase 1's `CALLS` resolution turns out harder than expected (dynamic dispatch, decorators), that's expected per the PRD risks — document the limitation and move on rather than over-investing before the eval harness can even tell you if it matters.
