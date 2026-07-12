# orlog — design principles

*Working title during design: "the epistemic kernel." Not a memory store, not an
output rail. A kernel for agent knowledge: the full lifecycle from raw experience
to trusted assertion, with an explicit state machine for what the system is
allowed to claim. Every existing tool covers one stage — stores hold, guardrails
filter, event logs record. None governs the whole path from "something happened"
to "I can assert this, and here is why." That path is the product.*

This document is the source of truth for the project. Code must not contradict it;
where code and this document disagree, this document wins and the code is the bug.

## The epistemic state machine

Everything below serves this. Knowledge moves through explicit states; a claim is
never left in limbo — it always ends VERIFIED or ABSTAINED, never "probably fine."

```
OBSERVED ──► INTERPRETED ──► RETRIEVED ──► ASSERTED ──► VERIFIED ──► REINFORCED
(logged)     (projected)     (as-of)       (derived,     (sovereign     (outcome
                                            must cite)     check)         weights)
                                                 │             │
                                                 │             └─ fail ─► RE-DERIVE ─► ABSTAINED
                                                 │                            │
                                                 │                     (retry once,
                                                 │                      then give up)
                                                 └──────────────────────────────────────
                                                    every claim ends VERIFIED or ABSTAINED
```

## Design principles

Each principle is paired with the evidence that earned it — none of this is
aesthetic preference; all of it was measured or observed.

1. **Ground truth is append-only; interpretation is disposable.** Never destroy
   what you might later reinterpret. The log is written once and scrubbed once;
   everything above it can be deleted and rebuilt.
   *Evidence: write-time-commit scored 0.00 on superseded-fact questions — the
   answers had been destroyed at ingestion. The projection scored 1.00 on the
   same questions.*

2. **Commit as late as possible.** Interpretation is a read-time (or background)
   concern, never an ingestion concern. The moment you consolidate at write time,
   you bet the system on today's model being right forever.
   *Evidence: the entire incumbent field (Mem0, A-MEM, MemMA) makes this bet; the
   supersession cell is the structural counterexample.*

3. **Every layer between log and boundary is allowed to be wrong.** Projections
   may misgroup, retrieval may miss, derivations may hallucinate — the
   architecture assumes it. Correctness is enforced at one place: the exit
   boundary. This is what makes cheap, imperfect models usable everywhere else.
   *Evidence: with 30% of the projection deliberately corrupted, accuracy held at
   95% and zero wrong answers were served.*

4. **Trust is a gate, not a property.** No component is "trusted" — every
   assertion passes the sovereign verifier, which is deterministic, external to
   the memory's own interpretation, and runs on every path including cached ones.
   Skip the derivation, never the verifier.
   *Evidence: the blind cache served 5/10 stale answers after facts changed; the
   gated cache served 0, while still saving half the derivation cost.*

5. **Fail visible, not plausible.** Abstention is a first-class terminal state,
   not an error. When verification cannot succeed, the system says "cannot
   verify" — it never falls back to its best guess.
   *Evidence: under deliberate retrieval collapse, the ungated system produced 9
   confident wrong answers; the gated system produced 1, converting the rest to
   honest abstentions. For high-stakes domains this failure-mode swap IS the
   product.*

6. **Learn from outcomes, not co-occurrence.** Importance, edge weights, cache
   priors — all adaptation is driven by what actually constrained a verified
   result, assigned retroactively. Correlation-based learning
   (fire-together-wire-together) is banned from the write path.
   *Evidence: co-occurrence Hebbian learning consistently degraded retrieval
   (F1 0.196 → 0.118); outcome-gating is the fix the data demanded.*

7. **Time is a field, not a footnote.** Every fact carries validity windows;
   every query carries an as-of point; the log is bi-temporal (when it happened
   vs. when we learned it). "Current" is just `as_of(now)`.
   *Evidence: temporal questions are where every memory system benchmarked was
   weakest, and where the projection's windows made the difference between 0.46
   and 1.00.*

8. **One stream.** User facts, agent steps, tool outputs, decisions — one event
   graph, one importance economy. No second memory system for "user profile"
   that drifts out of sync with operational memory.
   *Evidence: the personalization-vs-institutional split is a known unsolved
   seam in every incumbent system surveyed.*

9. **Determinism everywhere except the two model slots.** Projections are pure
   functions of the log (idempotent, replayable, diffable). Verification is
   deterministic by contract. Models appear in exactly two places —
   interpretation (building projections) and derivation (producing assertions)
   — and both are downstream-checked. Byte-identical replay is a feature, not a
   luxury.

10. **Memory governs action, not just context.** The kernel can gate actions,
    not only answers: before an agent repeats an approach, the outcome ledger is
    consulted — three failed attempts on this path is a warning, a verified
    success is a fast-path.

11. **Scrub by tokenization, never by deletion.** PII removal is the one
    irreversible write-time act, so it must not destroy ground truth: replace
    identities with stable pseudonyms so the projection can still reason over
    entities it cannot read.
    *Evidence: over-scrubbing is the only way this architecture can lose ground
    truth, so it gets its own principle.*

12. **The kernel protects trust; recall is a separate budget.** Verification
    cannot find memories retrieval missed. Retrieval quality (embeddings,
    reranking, candidate width) is an independent, measurable investment — never
    confuse "verified" with "complete."
    *Evidence: every system benchmarked missed roughly half the relevant
    evidence; the verifier's job was making those misses honest, not fixing
    them.*

## The framework

Six layers, each a pluggable interface. The kernel is the contracts between
them — implementations are replaceable.

```
L5  ADAPTATION    outcome ledger -> importance, cache priors, projection scores
L4  VERIFICATION  sovereign checker, every exit path, abstention flow
L3  DERIVATION    LLM assertions with mandatory citation
L2  RETRIEVAL     as-of-aware, pluggable (vector / lexical / graph)
L1  PROJECTION    pure log->view functions, versioned, concurrent
L0  LOG           append-only, scrubbed, bi-temporal
```

- **L0 — Log.** `append(event) -> EventId`, immutable, bi-temporal (`occurred_at`,
  `recorded_at`), scrub-on-ingest with a pseudonym registry. No other layer may
  write anywhere else. → `urd.py`

- **L1 — Projections.** `project(log_slice) -> View`, pure and idempotent. Views:
  supersession chains, entity graphs, session summaries, embedding indexes.
  Multiple projections coexist; migration is re-projection, rollback is
  re-projection with the old version. A projection carries its own version and a
  quality score (fed back from L5). → `verdandi.py`

- **L2 — Retrieval.** `retrieve(query, as_of, view, k) -> [Candidate]`. Pluggable
  backends. Contract: must respect validity windows; must report what it
  excluded (the no-silent-caps rule).

- **L3 — Derivation.** `derive(query, candidates) -> Assertion{claim, citations[]}`.
  The one hard rule: uncited assertions are rejected before they ever reach L4
  — an empty citation list must not pass vacuously through any checker. → `huginn.py`

- **L4 — Verification.** The contract that makes the kernel a kernel:
  `verify(citation, claim, as_of) -> Pass | Fail(reason)`. Deterministic.
  Grounded outside the memory's own interpretation (current law, a test suite, a
  rules engine, a schema). Full scope required: exists, currently valid,
  supports the claim. Runs on every exit path — fresh, cached, or
  action-gating. Failure triggers one re-derivation with the invalid citations
  removed; second failure is abstention. → `heimdall.py`

- **L5 — Adaptation.** The outcome ledger: every verified pass, every failure,
  every abstention, every user correction is itself an event (appended back to
  L0 — the loop closes). From it: retroactive importance weights on facts,
  cache-route priors, projection quality scores, action-gating warnings. The
  system learns only from outcomes (principle 6). → `skuld.py`

- **Cross-cutting — the route cache.** Keyed on derivation inputs, invalidated
  by verification outcomes — never by TTL. A cache hit still passes L4. This is
  what makes reuse provably safe. → `muninn.py`

## Why this isn't thin

A "verification layer" says "check outputs." This says: an agent's knowledge has
a lifecycle with enforceable states, and no claim reaches the world without
passing through it. The verifier is one gate in that lifecycle — the kernel is
the lifecycle itself: append-only ground truth, disposable interpretation, late
commitment, outcome-driven learning, honest failure. Each principle is
load-bearing.
