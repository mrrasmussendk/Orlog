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
