# Orlog vs Mem0 benchmark: speed, accuracy, abstention

Status: approved for planning
Date: 2026-07-17

## Motivation

orlog's whole pitch (`README.md`) is that it never returns a confident-sounding
guess — every claim is `VERIFIED` (checked against the raw log, every call) or
`ABSTAINED` (with a reason). Mem0 is the most visible existing "memory for AI
agents" project, and it takes a different approach: an LLM extracts facts on
write, an LLM (optionally) decides ADD/UPDATE/DELETE against existing
memories, and a query returns the nearest-scoring stored memories with no
built-in verified/abstained distinction.

There's no benchmark today comparing the two on anything concrete. This
project builds one: a small, reproducible harness that runs the same synthetic
facts and questions through both systems and reports speed, accuracy, and
abstention/false-positive behavior as charts.

## Goals

- A reusable, rerunnable benchmark harness under `benchmarks/vs_mem0/`,
  committed to the repo.
- Both systems driven through their real Python APIs (not mocked), using real
  OpenAI API calls (`OPENAI_API_KEY` is set in this environment;
  `ANTHROPIC_API_KEY` is not, so both systems are pointed at OpenAI —
  `gpt-5-mini` for both, for a same-model comparison).
- Metrics: ingest latency, query latency, accuracy on known facts (including
  facts that were later updated/superseded), abstention-vs-false-positive
  rate on questions about facts that were never stored, and a token-usage
  proxy for cost.
- An interactive HTML chart dashboard (Artifact) built from the run's results.
- A committed `results.json` snapshot from one real run, with an explicit
  note that exact numbers will drift slightly on reruns (LLM calls aren't
  perfectly deterministic).

## Non-goals

- No LLM-as-judge grading. Accuracy is graded by deterministic (case-insensitive)
  substring match of the expected value against the returned answer text —
  simple, reproducible, and sufficient because the synthetic dataset's values
  (plan names, cities, job titles) are unambiguous single tokens/phrases.
- No attempt to tune Mem0's parameters favorably or unfavorably — its shipped
  defaults are used throughout (`threshold=0.1`, `top_k=20` at the API
  default, standard "smart update" `infer=True` on add).
- No changes to `src/orlog/` — this is a new, additive `benchmarks/` tree only.
- No large-scale/standard benchmark corpus (e.g. LOCOMO/LongMemEval) — a
  purpose-built synthetic dataset is used instead, scoped to what orlog's
  reference domain (`{entity, attribute, value}` facts) and Mem0's
  conversational-memory model both handle natively.

## Dataset (`benchmarks/vs_mem0/dataset.py`)

Deterministic, hand-authored, no randomness — same content on every run.

- 10 fictional entities (`user:aiko`, `user:bilal`, ... — first names only,
  not real people), each with 4 attributes: `plan`, `city`, `job_title`,
  `favorite_language`. 40 initial facts.
- 10 update facts: for a chosen subset of (entity, attribute) pairs already
  above, a second, later, contradicting statement (e.g. plan `Pro` →
  `Enterprise`), added to each system *after* all 40 initial facts. Tests
  whether a later query returns the new value, not the stale one.
- 15 "unknown" questions: natural-language questions about an
  attribute/entity combination that was never stored (e.g. "What's Chen's
  shoe size?"). Tests abstention (orlog) vs. false-positive return (Mem0),
  since Mem0 has no verified/abstained concept and will return its
  nearest-scoring memory (if any clears `threshold=0.1`) even when nothing
  relevant was ever stored.

Each dataset item carries: a natural-language statement (used as the write
payload for both systems, plus orlog's required `entity`/`attribute`/`value`
triple), a natural-language question (used as the *free-text* query for both
systems — orlog's `recall_tool()` with no `.` in the query, so it exercises
the same semantic-resolve path Mem0's `search()` always uses), and, for known
facts, the expected current value used for grading.

## System wiring

**Orlog** (`benchmarks/vs_mem0/run_orlog.py`):
- Fresh temp `Workspace` per run, `ORLOG_VAULT_KEY` generated in-process.
- `OrlogConfig` with `deriver.backend="openai"`, `deriver.model="gpt-5-mini"`,
  `deriver.api_key_env="OPENAI_API_KEY"`, `retrieval.embedder="BAAI/bge-small-en-v1.5"`
  (the spec default — real semantic matching, comparable in spirit to Mem0's
  OpenAI embeddings).
- Writes via `remember_tool(runtime, text, entity=, attribute=, value=)`.
- Queries via `recall_tool(runtime, question)` (free-text path).
- The embedder's one-time cold-start model download (network-bound, not part
  of what's being measured) happens via a warm-up call before timing starts.

**Mem0** (`benchmarks/vs_mem0/run_mem0.py`):
- `mem0.Memory()` with default config: local on-disk Qdrant (no server
  needed — `qdrant-client`'s embedded/local mode), `llm` provider `openai`
  model `gpt-5-mini`, default embedder `text-embedding-3-small`.
- One Mem0 `user_id` per entity, mirroring orlog's per-entity keys.
- Writes via `m.add(text, user_id=entity)` (`infer=True`, Mem0's default —
  lets its own ADD/UPDATE/DELETE logic run, since that's the feature being
  tested for the update facts).
- Queries via `m.search(question, filters={"user_id": entity})`, top-1 result
  used for grading.

Both runners write raw per-item timings and outcomes to their own JSON file
(`orlog_results.json`, `mem0_results.json`); per-item exceptions are caught,
logged as a `"status": "error"` entry, and the run continues rather than
aborting.

## Scoring (`benchmarks/vs_mem0/score.py`)

Loads both raw result files and computes, per system:

1. Ingest latency: mean, p50, p95 (ms).
2. Query latency: mean, p50, p95 (ms), split into known-fact queries and
   unknown-fact queries (the unknown path can short-circuit differently on
   both systems, so it's worth seeing separately).
3. Accuracy on the 40 known-fact queries (post-update): substring match rate.
   The 10 queries touching an updated fact are also broken out as their own
   "post-update accuracy" number.
4. Abstention/false-positive rate on the 15 unknown queries: orlog counts
   `abstained=True` as correct; Mem0 counts "no result cleared its own
   default threshold" as correct — anything else counts as a false positive.
5. Token usage total (orlog: `Assertion.tokens`; Mem0: usage from its
   response if exposed, else omitted with a note).

Output: a combined `benchmarks/vs_mem0/results.json` (raw + aggregated) and a
short printed summary table.

## Output / charts

An HTML dashboard, built via the `dataviz` skill's conventions, published as
an Artifact: latency bar charts (ingest/query, both systems), an accuracy bar
chart (overall + post-update-only), an abstention/false-positive bar chart,
and a small token-usage chart. Loaded from the committed `results.json`, not
regenerated by chart code — so republishing the dashboard later doesn't
require rerunning the benchmark.

## Error handling

- Missing `OPENAI_API_KEY` at script start → fail fast with a clear message
  (already confirmed present in this environment).
- Any single item's API call failing (timeout, rate limit, malformed
  response) → caught, recorded as an error entry, counted separately in the
  summary, does not abort the run.
- If every item for a system errors out, `score.py` reports that system's
  metrics as unavailable rather than dividing by zero.

## Testing

This is a benchmark script, not library code with a pytest suite — its
"test" is a real run producing sane, non-empty `results.json` output and a
renderable dashboard. `benchmarks/vs_mem0/README.md` documents how to rerun
it and the grading caveats above.
