# Orlog vs Mem0 benchmark: speed, accuracy, abstention

Status: approved for planning
Date: 2026-07-17

## Known deviations found by actually running the benchmark

This spec originally called for `gpt-5-mini` throughout (see Goals), with
zero changes to `src/orlog/` (see Non-goals). Two real, provider-independent
problems surfaced only from actually executing Task 8 end to end, both
documented in full in `benchmarks/vs_mem0/README.md`'s "Model note":

1. **`gpt-5-mini` incompatibility.** It rejects any non-default
   `temperature` and the legacy `max_tokens` parameter — incompatible with
   both orlog's `OpenAICompletion` (hardcoded `temperature=0`) and Mem0's
   default extraction (`temperature=0.1`). Both runners were switched to
   `gpt-4o-mini`.

2. **orlog's real-LLM derivation path failed near-universally, on every
   model tested, on both providers, including orlog's own README example**
   — not a model-choice problem at all. Root cause: `Candidate.content`
   (`src/orlog/retrieval.py`) gave the deriver only a fact's bare value,
   with no attribute label or grounding text, and the deriver's
   `SYSTEM_PROMPT` never told the model it had to restate the literal
   `"{entity}.{attribute}"` key in its claim (a requirement heimdall's V4
   SUPPORTS check deliberately enforces, for good reason — see
   `heimdall.py`'s own module docstring). This is a real bug in orlog
   itself, not a benchmark-side issue, and the benchmark's own
   "zero changes to `src/orlog/`" non-goal was explicitly lifted for this
   one fix, with the user's direct approval, once evidence ruled out every
   other explanation. Fixed with two small, additive changes (`Candidate`
   gained an `excerpt` field already anticipated by spec §3.4; the system
   prompt now instructs the model to restate the question verbatim) —
   orlog's full test suite (287 tests) passes unchanged, and
   `Candidate.content`/ScriptedDeriver's claim format are untouched.

With both fixed, the final run uses orlog on Anthropic (`claude-haiku-4-5`,
its own calibrated default) and Mem0 on OpenAI (`gpt-4o-mini`) — cross-
provider rather than same-model, since that was the working combination
available when this was diagnosed. Every other design decision below
(dataset, shared-scope Mem0 bucket, substring grading, metrics) is
unchanged.

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
  `native_language`. 40 initial facts.
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
- `OrlogConfig` with `deriver.backend="anthropic"`, `deriver.model="claude-haiku-4-5"`,
  `deriver.api_key_env="ANTHROPIC_API_KEY"`, `retrieval.embedder="BAAI/bge-small-en-v1.5"`
  (the spec default — real semantic matching, comparable in spirit to Mem0's
  OpenAI embeddings).
- Writes via `remember_tool(runtime, text, entity=, attribute=, value=)`.
- Queries via `recall_tool(runtime, question)` (free-text path).
- The embedder's one-time cold-start model download (network-bound, not part
  of what's being measured) happens via a warm-up call before timing starts.

**Mem0** (`benchmarks/vs_mem0/run_mem0.py`):
- `mem0.Memory()` with default config: local on-disk Qdrant (no server
  needed — `qdrant-client`'s embedded/local mode), `llm` provider `openai`
  model `gpt-4o-mini`, default embedder `text-embedding-3-small`.
- **All entities share a single Mem0 `user_id`** (`"vs_mem0_bench"`), not one
  per entity. Reasoning: Mem0's `search()` requires a `user_id`/`agent_id`/
  `run_id` filter and only searches within that partition — giving each
  entity its own `user_id` would let Mem0 search a pre-scoped ~5-fact pool
  while orlog's free-text `recall()` has no such scoping and must
  semantically disambiguate the right entity out of the *entire* shared
  event log. A single shared bucket for Mem0 mirrors orlog's single global
  log, so both systems face the same challenge: given a natural-language
  question, find the right person's fact among all of them. This also makes
  the update test harder (and more realistic) for Mem0, since its
  ADD/UPDATE/DELETE decision has to correctly match a new statement to the
  right prior memory among all 40+ stored ones, not just among one entity's
  four.
- Writes via `m.add(text, user_id="vs_mem0_bench")` (`infer=True`, Mem0's
  default — lets its own ADD/UPDATE/DELETE logic run, since that's the
  feature being tested for the update facts).
- Queries via `m.search(question, filters={"user_id": "vs_mem0_bench"})`,
  top-1 result used for grading.

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

Pure logic is covered by pytest: `tests/test_benchmark_dataset.py`,
`tests/test_benchmark_grading.py`, `tests/test_benchmark_score.py`, and
offline smoke tests for the orlog runner in `tests/test_benchmark_run_orlog.py`.
However, the actual API-calling orchestration (`main()` in both `run_orlog.py`
and `run_mem0.py`) is only exercised by a real run; no automated no-API-key
test exists for Mem0's orchestration specifically, since Mem0 has no offline
mode. A real run produces sane, non-empty `results.json` output and a
renderable dashboard. `benchmarks/vs_mem0/README.md` documents how to rerun
it and the grading caveats above.
