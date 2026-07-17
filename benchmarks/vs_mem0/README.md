# orlog vs Mem0 benchmark

Compares [orlog](../../README.md) and [Mem0](https://github.com/mem0ai/mem0)
on speed, accuracy, and abstention/false-positive behavior, using a
deterministic 10-entity/4-attribute synthetic dataset (`dataset.py`).

Full design rationale:
[`docs/superpowers/specs/2026-07-17-vs-mem0-benchmark-design.md`](../../docs/superpowers/specs/2026-07-17-vs-mem0-benchmark-design.md).

## Running it

Requires `OPENAI_API_KEY` in the environment (both systems use OpenAI --
`gpt-5-mini` -- as their LLM backend, since no Anthropic key was configured
when this benchmark was built) and the `benchmark` extra installed:

```
python -m pip install -e ".[benchmark]"
python -m benchmarks.vs_mem0
```

This makes real, billed OpenAI API calls: one derivation call per orlog
query, one extraction (+ possible update-decision) call per Mem0 `add()`.
At this benchmark's scale (40 initial facts + 10 updates + 55 questions per
system) that's on the order of 100-150 LLM calls total, well under $1 with
`gpt-5-mini`.

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
