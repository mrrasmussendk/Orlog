# Phase 0 benchmark — before and after the §B12 seams

What the five seams of `ORLOG-SPEC.md` §B12 cost and save, measured
through orlog's own tool surface (`orlog.server_tools`) rather than
against internals, so the same script runs unmodified on a pre-Phase-0
checkout and a post-Phase-0 one.

No API keys and no network: the deriver is `ScriptedDeriver` and the
embedder is `hashing`, so every number here is orlog's own machinery. This
is **not** the [orlog vs Mem0 benchmark](../vs_mem0/README.md), which
measures accuracy and abstention against another system and does need real
keys.

Charts: [`dashboard.html`](dashboard.html) — open it in a browser.

## Running it

```bash
# on a pre-Phase-0 checkout
python -m benchmarks.phase0.bench --out before.json
# on this one
python -m benchmarks.phase0.bench --out after.json
python -m benchmarks.phase0.compare before.json after.json --out results.json
```

`bench.py` records `orlog_has_index_query_api` in each report and
`compare.py` refuses a mislabelled pair, so a before/after can't be
accidentally reversed.

The context and correctness numbers below come from a second script, which
needs only one checkout:

```bash
python -m benchmarks.phase0.context --out context.json
```

## Context per answer

How much context one answer costs an agent, against the baseline an agent
without a memory layer actually falls back to — re-reading the record.
Tokens are chars ÷ 3.5, the estimator the Mímir plan uses for the same job.

| facts | `recall` | re-read the log | reduction | precision of a re-read |
|------:|---------:|----------------:|----------:|-----------------------:|
| 40 | 116 | 4,777 | 41× | 2.43% |
| 400 | 116 | 47,199 | 407× | 0.25% |
| 2 000 | 116 | 237,257 | 2 045× | 0.05% |
| 10 000 | 116 | 1,191,542 | 10 272× | 0.01% |

`recall` returns one verified claim with its citations: **116 tokens at
every scale**, all of them about the fact that was asked for. A re-read
delivers one relevant fact among n, so its context precision is 1/n by
token mass — 2.43% at 40 facts, 0.01% at 10 000.

Phase 0 did not change these numbers: the tools return the shapes they
always did. This is what orlog is *for*. What the seams changed is the cost
of producing that answer, which is the table above.

## Verify or abstain

55 questions over [`vs_mem0`'s dataset](../vs_mem0/dataset.py) — 40 facts
with 10 later superseded, and 15 that were never stored — asked by exact
key with the ScriptedDeriver, so neither a model's extraction nor an
embedding's recall is in the loop. What is measured is the deterministic
verify-or-abstain pipeline.

| | n | result |
|---|--:|---|
| stable facts | 30 | 30 served and verified |
| superseded facts | 10 | 10 served the **current** value, not the original |
| never stored | 15 | 15 abstained |
| **confidently wrong** | — | **0 of 55** |

Free-text accuracy against Mem0 is a different question and lives in
[`vs_mem0`](../vs_mem0/README.md); it needs API keys and makes billed calls.

## Does consulting it change the decision?

`handoff.py` runs the narrow, deterministic core of "is this memory
useful": a second agent picks up work after the first one's context is
gone and acts on twelve facts about a project.

| arm | correct | stale | abstained |
|---|--:|--:|--:|
| A — a `/compact`-style summary | 6 | **4** | 2 |
| B — asks `recall()` | **10** | **0** | 2 |

Arm A is not a hand-built straw man: it is `recall(key,
known_as_of=<the instant the summary was written>)` — what the record
knew then, from the same call site as arm B with one argument changed. A
control the treatment can quietly beat is not worth asserting on.

The fixture plants cases orlog loses. Six of the twelve facts never
change, so consulting it only confirms what the summary already had. Two
were never recorded, and orlog does not rescue them — it abstains, which
is a better failure than a confident wrong answer but is still a failure
to answer. Both arms end up equally stuck on those two.

**What this does not establish:** that an agent handed current facts does
better *work*. There is no model here and no judgment. That is the Mímir
plan's Phase 6 eval — fixture repo, three live arms, planted rules, task
success — and nothing here substitutes for it.

`tests/test_handoff_usefulness.py` asserts these properties in both
directions, so the file fails if arm A ever stops going stale or if orlog
ever starts answering something nobody wrote down. Reversing
`build_supersession_chains`' sort order fails 4 of its 10 tests.

## Results

Python 3.12, Linux, 50 entities × 8 attributes, 30 runs per query, p50.
Raw numbers in [`results.json`](results.json).

| events | operation | before | after | speedup |
|-------:|-----------|-------:|------:|--------:|
| 250 | ingest | 105.3 ev/s | 255.2 ev/s | **2.4×** |
| | `recall` | 14.59 ms | 7.97 ms | 1.8× |
| | `recall_history` | 4.31 ms | 0.02 ms | **196×** |
| | `list_entities` | 4.38 ms | 0.11 ms | **39×** |
| | `list_attributes` | 4.33 ms | 0.06 ms | **69×** |
| | `check_action` | 4.21 ms | 0.01 ms | **301×** |
| 1 000 | ingest | 39.5 ev/s | 256.8 ev/s | **6.5×** |
| | `recall` | 59.58 ms | 20.44 ms | 2.9× |
| | `recall_history` | 17.74 ms | 0.07 ms | **261×** |
| | `list_entities` | 22.13 ms | 0.28 ms | **78×** |
| | `list_attributes` | 24.75 ms | 0.25 ms | **100×** |
| | `check_action` | 17.61 ms | 0.01 ms | **1354×** |
| 2 500 | ingest | 17.2 ev/s | 245.3 ev/s | **14.3×** |
| | `recall` | 145.61 ms | 46.99 ms | 3.1× |
| | `recall_history` | 50.71 ms | 0.13 ms | **384×** |
| | `list_entities` | 59.66 ms | 0.45 ms | **133×** |
| | `list_attributes` | 54.71 ms | 0.59 ms | **94×** |
| | `check_action` | 55.51 ms | 0.02 ms | **3265×** |

The shape matters more than any single row. **Before, every number grows
with the size of the workspace**, because every operation read and
re-parsed the whole log: ingest falls from 105 to 17 events/second between
250 and 2 500 events, and `check_action` — a governance check that counts
two kinds of row — goes from 4 ms to 56 ms. **After, the read tools are
flat**: `check_action` is 0.014 ms at 250 events and 0.017 ms at 2 500,
and ingest holds ~250 events/second at every scale. The speedups grow with
N because the old cost was O(N) per call and the new one is not; at the
100 k events of §B12.4's acceptance criterion, `list_entities` is 0.7 ms.

## `recall` is the one that did not get flat

`recall` improved 1.8–3.1×, and it still grows with N. That is worth being
precise about rather than hiding in an average. Profiling one `recall` on
a 2 500-event workspace, 20 runs:

```
query.fact_events()  [index read]    22.53 ms
projection_store.view()  [cached]     0.00 ms
build_ground_truth()                 15.27 ms
events_by_id dict build               0.13 ms
build_pipeline()  [whole]            33.36 ms
recall_tool()  [whole]               41.92 ms
```

The projection cache (§B12.2) is doing its job completely — a cached view
costs nothing measurable. What remains is two O(N) passes Phase 0 did not
set out to remove: re-materializing every fact `Event` out of the index on
each call (22.5 ms), and rebuilding the verifier's ground-truth table over
all of them (15.3 ms). Together they are 38 of the 42 ms.

Both are cacheable on exactly the key the projection view already uses
(projection, horizon, source head id, source length), which is the obvious
next step — and the one to take before the Mímir plan's Phase 4 target of
"p95 under 200 ms at 50 k events", since on this curve `recall` would not
meet it.

## Why ingest got faster

Three separate O(N)-per-write costs went away:

- `SegmentedLog._write_head()` called `event_count()` on every append,
  which re-read and re-parsed every segment — so appending N events cost
  O(N²) line parses. The count is now carried.
- `Runtime._known_details_for_label()` read the whole log on every
  `remember()` to enforce §B9's entity_detail rule. It is an indexed
  lookup now.
- `_known_attributes_for_type()` did the same when `[schema] known_types`
  is set.
