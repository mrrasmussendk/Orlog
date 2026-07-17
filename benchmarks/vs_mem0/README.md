# orlog vs Mem0 benchmark

Compares [orlog](../../README.md) and [Mem0](https://github.com/mem0ai/mem0)
on speed, accuracy, and abstention/false-positive behavior, using a
deterministic 10-entity/4-attribute synthetic dataset (`dataset.py`).

Full design rationale:
[`docs/superpowers/specs/2026-07-17-vs-mem0-benchmark-design.md`](../../docs/superpowers/specs/2026-07-17-vs-mem0-benchmark-design.md).

## Running it

Requires both `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` in the environment
(orlog uses Anthropic's `claude-haiku-4-5` -- its own calibrated default,
see "Model note" below; Mem0 uses OpenAI's `gpt-4o-mini`) and the
`benchmark` extra installed:

```
python -m pip install -e ".[benchmark]"
python -m benchmarks.vs_mem0
```

This makes real, billed API calls: one derivation call per orlog query
(Anthropic), one extraction (+ possible update-decision) call per Mem0
`add()` (OpenAI). At this benchmark's scale (40 initial facts + 10 updates +
55 questions per system) that's on the order of 100-150 LLM calls total,
well under $1 combined.

**Model note (two real bugs found and fixed along the way):**

The original design called for both systems on the same OpenAI model
(`gpt-5-mini` first, then `gpt-4o-mini`) for a strict same-model comparison.
Two real, provider-independent problems surfaced from actually running it,
both fixed in `src/orlog/` rather than worked around from the benchmark
side, since they affect orlog generally, not just this benchmark:

1. **`gpt-5-mini` rejects any non-default `temperature`** via the Chat
   Completions API ("Only the default (1) value is supported") and also
   rejects the legacy `max_tokens` parameter. Both orlog's
   `OpenAICompletion` (hardcoded `temperature=0`, by design) and Mem0's
   default extraction (`temperature=0.1` internally) hit this on every
   call. Fixed by switching both runners to `gpt-4o-mini`.

2. **orlog's real-LLM derivation path returned `INSUFFICIENT`/`UNSUPPORTED`
   on nearly every query, on every model tested (5 real models across both
   providers, including orlog's own README example).** Root cause, found
   by isolating the exact prompt orlog sends: `retrieve_current_fact()`
   (`src/orlog/retrieval.py`) only ever gave the deriver a fact's bare
   stored value (e.g. `"Pro"`), with no attribute label or grounding
   sentence -- genuinely insufficient for a careful model to verify against
   a terse `"Question: user:alice.plan"` prompt, regardless of the model's
   capability. Separately, once the deriver *did* produce a claim, heimdall's
   V4 SUPPORTS check (`src/orlog/heimdall.py`) -- which deliberately
   requires the claim to literally contain both the fact's own
   `"{entity}.{attribute}"` key and its value, to catch a claim built from a
   wrong-but-plausible citation -- was rejecting well-formed natural-language
   claims that never restated the literal key. Fixed with two small,
   additive changes: `Candidate` gained an `excerpt` field (spec §3.4
   already anticipated this field; the runtime just never populated it)
   carrying the fact's remembered evidence text, and `huginn_llm.py`'s
   `SYSTEM_PROMPT` now explicitly instructs the model to restate the
   question verbatim in its claim. Verified against real Anthropic and
   OpenAI calls before and after; orlog's full test suite (283 tests) passes
   unchanged. Since `Candidate.content` and ScriptedDeriver's own claim
   format are untouched, this is additive to the LLM-derivation path only.

With both fixes in place, orlog was run on Anthropic (its calibrated
default) rather than OpenAI for the final result -- not because OpenAI
still fails (it doesn't, post-fix), but because the Anthropic run was the
one actually available when this was diagnosed. Mem0 stayed on OpenAI. This
makes the final comparison cross-provider (each system on a provider that
works for it) rather than same-model; see the design spec's "Known
deviation" notes for the full history.

Writes `orlog_results.json`, `mem0_results.json` (raw per-item timings and
outcomes) and `results.json` (aggregated metrics) into this directory.

## Methodology

- **Same inputs, both systems.** Every fact's natural-language statement is
  fed to both `orlog.remember()` and `mem0.add()`; every question is fed to
  both `orlog.recall()` (free-text path) and `mem0.search()`.
- **Single shared scope.** All 10 entities live in orlog's one event log and
  in one shared Mem0 `user_id`. Neither system is told at query time which
  entity a question is about -- it has to work that out from the question
  text itself, the same challenge either way.
- **Grading is a deterministic, case-insensitive substring match**
  (`grading.is_correct`) between the expected value and whatever text the
  system returned -- not an LLM judge. This works cleanly here because every
  attribute's value pool (`dataset.py`) is constructed so no value is a
  substring of another, but it is a simplification worth knowing about
  before generalizing these numbers beyond this dataset.
- **Abstention/false-positive rate** on the 15 "unknown" questions (about
  attributes never stored for that entity): orlog counts a `NO_CANDIDATES`/
  other abstention as correct; Mem0 counts "no result cleared its own
  default `threshold=0.1`" as correct. Mem0 has no built-in
  verified/abstained concept -- this metric is specifically about whether it
  ever hands back a stored memory in response to a question it has no real
  answer to.
- **Mem0's defaults are used as-is** (`threshold=0.1`, `infer=True`,
  `version="v1.1"`) -- nothing is tuned in either system's favor.
- **Numbers will drift slightly between runs.** Real LLM calls aren't
  perfectly deterministic; the committed `results.json` is one real
  snapshot, not a guarantee reproduced exactly on every rerun.
