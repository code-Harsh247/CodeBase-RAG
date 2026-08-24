# Product Requirements Document

**Project (working name):** CodeGraph — Graph-Augmented Codebase Q&A
**Author:** Harsh Chattar
**Status:** Draft v2 — scoped to Python only
**Last updated:** 2026-08-24

---

## 1. Problem Statement

Given a public GitHub repository, engineers want to ask natural-language questions about the codebase ("what calls this function", "how does auth flow through the system", "what would break if I change this interface") and get answers grounded in the actual structure of the code — not answers hallucinated from superficially similar-looking text.

Naive RAG (chunk the repo, embed, cosine-similarity retrieve) fails at this because:
- Code meaning is relational (call graphs, imports, inheritance), and chunking destroys that structure.
- Similarity search finds text that *reads* similar, not code that is *structurally* relevant.
- Multi-hop questions ("what depends on X, and what does X depend on") require graph traversal, not single-shot retrieval.

## 2. Goals

1. **Primary (portfolio) goal:** produce a project that demonstrates a real understanding of *why* naive RAG fails for code, and a concrete, measured fix — suitable to show recruiters/interviewers and defend in a technical conversation.
2. Given any public Python GitHub repo, let a user ask natural-language questions and get accurate, cited answers (`file:line`).
3. Quantitatively prove the approach beats naive RAG on a fixed evaluation set, not just claim it.

### Non-goals (explicitly out of scope for v1)
- Private repo support / auth-gated ingestion.
- Real-time collaborative multi-user usage.
- Runtime/dynamic tracing (e.g. eBPF call tracing) — noted as a credible future extension, not built now.
- Code editing / agentic write-actions (this is a **read/query** tool, not an IDE agent). Explicitly not competing with Cursor on editing — only on codebase *understanding*.
- Any language other than Python. The schema is *designed* to be language-agnostic, but only a Python mapper is built; adding a second language is a post-MVP extension, not a v1 deliverable.

## 3. Target Users

- **Primary:** the author, as a portfolio/demo piece for job applications and interviews.
- **Secondary (real usage):** developers onboarding onto an unfamiliar public repo who want faster orientation than manual code reading.

## 4. Scope

### MVP (must-have)
- Ingest a public GitHub repo URL: clone, parse with tree-sitter, build a knowledge graph in Neo4j.
- Support **Python** only.
- Graph schema covering: File, Module, Class, Function, Method, Import nodes; CONTAINS, DEFINES, CALLS, IMPORTS, INHERITS, REFERENCES edges.
- Natural-language Q&A via an agent that can:
  - Generate and execute Cypher queries against the graph.
  - Fall back to vector search (function/class embeddings) for fuzzy/conceptual questions.
  - Fall back to grep/file-read for anything neither structured layer resolves.
- Answers include citations (`file:line`) traceable to source.
- A basic UI (chat-style) to ask questions against an ingested repo.
- An evaluation harness with a fixed Q&A ground-truth set across 2-3 Python test repos, reporting precision/recall, **and a baseline comparison against naive chunk+embed RAG on the same questions**.

### Stretch (nice-to-have, time permitting)
- A second language (would demonstrate the schema really is language-agnostic — currently that is a design property, not a proven one).
- Incremental re-indexing on git diff instead of full re-parse.
- Graph visualization in the UI (beyond Neo4j Browser).
- Hosted live demo (Neo4j AuraDB free tier + deployed backend) so recruiters can try it without local setup.

### Explicitly deferred
- Runtime/dynamic call tracing.
- Multi-repo / cross-repo querying.
- Auth, multi-tenancy, private repos.

## 5. Functional Requirements

| ID | Requirement |
|----|-------------|
| FR1 | User submits a public GitHub URL; system clones and ingests it end-to-end without manual intervention. |
| FR2 | System parses Python source via tree-sitter and populates the graph schema (see Architecture doc). |
| FR3 | User asks a free-text question; system returns an answer with source citations. |
| FR4 | System selects between graph query, vector search, and raw file search based on question type (agentic tool selection, not hardcoded routing). |
| FR5 | System supports multi-hop questions (e.g., "what calls X, and what does X call") via iterative tool use, not single-shot retrieval. |
| FR6 | System provides an eval mode: run the fixed Q&A set, report per-question and aggregate precision/recall, and compare against a naive-RAG baseline implementation. |
| FR7 | Ingestion is idempotent — re-ingesting the same repo/commit produces the same graph. |

## 6. Non-Functional Requirements

- **Setup friction:** a reviewer should be able to run this locally with `docker-compose up` + one ingest command in under 10 minutes.
- **Ingestion time:** a mid-sized repo (~10-50k LOC) should ingest in a few minutes, not tens of minutes.
- **Query latency:** answers returned in well under 30s for typical questions (agentic multi-hop may take longer than single-shot, that's acceptable and should be shown, not hidden).
- **Correctness over completeness:** prefer one language that works reliably, with limitations documented, over broad language claims that are only partially true.
- **Transparency:** the UI should show *which tool(s)* were used to answer a question (graph query text, vector hits, files grepped) — this is a differentiator worth surfacing, not hiding as an implementation detail.

## 7. Success Metrics

Since this is a portfolio project, "success" = a defensible, demonstrable result:

1. Evaluation harness shows a **measured improvement** in answer/retrieval accuracy vs. a naive-RAG baseline on the same fixed question set (target: meaningfully higher F1/precision on multi-hop and relational questions — exact target TBD once baseline is measured, not fabricated in advance).
2. Python working end-to-end on at least 3 real public repos of varying size and layout.
3. A README with: architecture diagram, demo GIF/screenshots, and the eval results table — the artifact recruiters will actually look at.
4. Able to verbally defend every architectural decision in this document in an interview setting (why graph over pure vector, why Neo4j, why agentic over single-shot, what the eval methodology proves and its limitations).

## 8. Milestones

| Phase | Deliverable |
|-------|-------------|
| 1 | Ingestion pipeline: tree-sitter → schema → Neo4j, Python only. Verified via direct Cypher queries. |
| 2 | NL → Cypher agent, single-shot Q&A working end-to-end. |
| 3 | Add vector fallback + agentic multi-tool loop (multi-hop). |
| 4 | Eval harness + naive-RAG baseline + comparison writeup. |
| 5 (stretch) | UI polish, hosted demo, a second language, incremental indexing. |

## 9. Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Tree-sitter edge cases (dynamic imports, macros, decorators) produce incorrect graph edges | Scope schema conservatively; document known limitations rather than silently mis-representing them; cover in eval harness. |
| NL→Cypher generation produces invalid or unsafe queries | Validate generated Cypher is read-only before execution; constrain query generation with schema-aware prompting and few-shot examples; sandbox execution. |
| Eval results don't clearly favor the graph approach | This is a real possible outcome, not just a risk to "manage" — report it honestly if so; a well-reasoned negative/mixed result is still a legitimate, defensible portfolio artifact. |
| Scope creep toward matching code-graph-rag's full feature set (13 languages, eBPF tracing) | This PRD's non-goals section is the guardrail; revisit only after MVP + eval results are complete. |
| Single-language scope reads as thin to a reviewer | Depth is the answer: measured resolution quality, documented limitations, and a real eval beat a shallow language count. Be ready to say why in an interview. |
| Free-tier LLM quota blocks development or corrupts the eval run | Gemini's free tier (20 requests/day) was ruled out for exactly this reason before any agent code was written. Groq's free tier (1,000/day, 8,000 tokens/min) is the default for development; a small reserved paid budget (~$5-10) backs up the Phase 4 eval run specifically, so the project's headline result is never silently degraded by a quota. See ARCHITECTURE.md §2.4a. |

## 10. Open Questions

- Exact target repos for the eval set — pick 2-3 public Python repos of varying size and layout (flat vs. `src/`) once ingestion is working, to keep eval grounded in real behavior rather than designed to flatter the system.
- Whether the hosted demo (stretch) is worth the AuraDB free-tier limits for the audience size expected (recruiters spot-checking, not sustained traffic).
