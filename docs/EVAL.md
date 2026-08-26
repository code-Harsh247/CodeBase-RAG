# Evaluation

CodeGraph's central claim is that grounding an LLM in a knowledge graph of a
codebase, with agentic tool use, answers questions more accurately than either
a naive chunk-and-embed RAG baseline or a single generated Cypher query. This
is measured, not assumed — every answer is graded against a hand-written
reference, checked against the source at a pinned commit, across two
structurally different repositories.

## Headline results

| repo | naive RAG | single Cypher query | **multi-hop agent** |
|---|---|---|---|
| [psf/requests](https://github.com/psf/requests) — HTTP client, function-call-heavy | 41% | 27% | **86%** |
| [pallets/click](https://github.com/pallets/click) — CLI framework, decorator- and class-heavy | 55% | 27% | **82%** |

22 hand-written questions per repo, three categories each (structural,
multi-hop, conceptual), same model and grader across every system so a score
gap reflects retrieval strategy, not model capability. The two repos were
chosen to be different in shape — a flat HTTP client versus a class-heavy
decorator framework — so the result is evidence the approach generalizes
rather than a number tuned to one codebase.

## By category

| | requests: naive / 1-query / agent | click: naive / 1-query / agent |
|---|---|---|
| structural | 3/8 · 6/8 · 8/8 | 4/8 · 5/8 · 8/8 |
| multi-hop | 3/6 · 0/6 · 4/6 | 4/6 · 0/6 · 5/6 |
| conceptual | 3/8 · 0/8 · 7/8 | 4/8 · 1/8 · 5/8 |

The single-query system's collapse on conceptual questions (0/8 on requests,
1/8 on click) is the clearest evidence for why this project exists: a
question phrased as behaviour ("how does X decide Y") has no single Cypher
query that answers it — it needs semantic search to find the relevant code,
then reading it, sometimes across more than one hop. One-shot graph querying
cannot do that by construction, not by degree.

## Retrieval quality, independent of answer grading

Correctness (above) is graded by an LLM against a reference answer. Retrieval
is scored separately and mechanically — no LLM involved — against the exact
source locations that genuinely answer each question:

| repo | recall | precision |
|---|---|---|
| requests | 0.80 | 0.34 |
| click | 0.85 | 0.37 |

Recall is how much of the necessary evidence the agent actually surfaced;
precision is how much of what it surfaced was relevant, not noise filling the
model's context. The two are kept apart deliberately — a system can retrieve
the right code and still answer badly, and conflating the two would hide that.

## Methodology

- **Ground truth is written from source, never from either system's output.**
  Every reference answer was produced by reading the pinned commit directly
  and confirming the claim against the code — a ground truth derived from a
  system's own output would measure self-consistency, not correctness.
- **The grader must cite verbatim evidence.** Every non-"correct" verdict
  requires a direct quote from the answer being graded, checked
  programmatically against that text. If the quote doesn't actually appear —
  the grader citing something it never wrote — the question is re-graded once
  automatically rather than trusted; a verdict that still doesn't ground on
  the second attempt is flagged in the run's report rather than silently
  counted. This exists because it caught a real case: an LLM grader once
  justified a "wrong" verdict by claiming an answer had two items in the
  opposite order from what the answer actually said.
- **Extra detail is not penalized unless it's identifiably wrong.** A correct
  answer that adds true detail beyond the reference should not be marked down
  for it; the grading rubric says so explicitly, and only downgrades a claim
  the grader can point to as actually incorrect.
- **Commits are pinned.** Each question file records the exact commit its
  line numbers and facts were written against (`evaluation/questions/*.json`),
  and the local checkout is matched to it before scoring — a moving default
  branch would silently drift the ground truth's line numbers out from under
  the eval.

## Cost

~815K tokens combined across both full sweeps (naive RAG + single-query +
multi-hop agent, 22 questions each), at OpenRouter's published rate for
`qwen/qwen3-coder` — roughly **$0.80** for both repositories end to end.

## Reproducing

```bash
python cli.py eval --questions evaluation/questions/requests.json --out evaluation/results.json
python cli.py eval --questions evaluation/questions/click.json --out evaluation/results_click.json
```

Requires the target repo already ingested (`python cli.py ingest <url>`) and
checked out at the commit recorded in the question file. Raw per-question
results — including every answer, grade reason, and retrieval score — are in
`evaluation/results.json` and `evaluation/results_click.json`.
