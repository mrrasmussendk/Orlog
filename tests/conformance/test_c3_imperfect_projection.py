"""Conformance cell C3: imperfect projection (ORLOG-SPEC.md §6).

PASS criterion (spec table): 0 confidently-wrong answers served at 30%
corruption; accuracy on the remaining (uncorrupted) entities >= 90% of the
clean baseline.

Corruption model: verdandi's chain grouping is deliberately broken for ~30%
of entities, cross-wiring each corrupted entity's chain to point at a
DIFFERENT entity's real event id. This is not a fabricated/nonexistent
citation (that's C4's job) -- it's a believable, realistic projection bug:
grouping went wrong, but every citation heimdall sees is still a real,
currently-valid fact in the log. heimdall's V4 check must catch it anyway,
because heimdall computes its ground truth independently, straight from the
raw log, never from verdandi's (corrupted) view -- see heimdall.py's module
docstring, and the V4-key-mismatch test in tests/test_heimdall.py this cell
exercises end to end.
"""

import copy
from datetime import datetime, timezone

from orlog.heimdall import Heimdall, build_ground_truth
from orlog.huginn import ScriptedDeriver
from orlog.muninn import RouteCache
from orlog.pipeline import Pipeline
from orlog.skuld import OutcomeLedger
from orlog.verdandi import build_supersession_chains

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
ENTITY_COUNT = 10
CORRUPT_EVERY = 3  # corrupts entities 0, 3, 6, 9 -- 4 of 10, ~30%


def _build_log(make_log, make_event):
    log = make_log()
    for i in range(ENTITY_COUNT):
        log.append(
            make_event(
                occurred_at=NOW,
                payload={"entity": f"user:{i}", "attribute": "plan", "value": f"plan-{i}"},
            )
        )
    return log


def _corrupt_chains(view, corrupted_keys):
    # Cross-wire each corrupted key to the NEXT entity's real chain -- a
    # believable grouping bug, not a fabricated event id. Every wrong_key
    # lookup reads from the ORIGINAL (uncorrupted) chains, so corruptions
    # don't cascade into each other even when they wrap around.
    keys = sorted(view.chains.keys())
    corrupted = copy.deepcopy(view.chains)
    for key in corrupted_keys:
        wrong_key = keys[(keys.index(key) + 1) % len(keys)]
        corrupted[key] = view.chains[wrong_key]
    return view.model_copy(update={"chains": corrupted})


def test_corrupted_grouping_never_serves_a_confidently_wrong_answer(make_log, make_event):
    log = _build_log(make_log, make_event)
    events = log.read_all()

    view, pv = build_supersession_chains(events, builder="test", built_at=NOW)
    truth = build_ground_truth(events)  # independent of the view -- never corrupted

    corrupted_entities = {i for i in range(ENTITY_COUNT) if i % CORRUPT_EVERY == 0}
    corrupted_keys = {f"user:{i}::plan" for i in corrupted_entities}
    view = _corrupt_chains(view, corrupted_keys)

    pipeline = Pipeline(
        view=view,
        events_by_id={e.id: e for e in events},
        projection_version=pv,
        cache=RouteCache(),
        deriver=ScriptedDeriver(),
        verifier=Heimdall(truth, truth_version="test-v1"),
        ledger=OutcomeLedger(log),
    )

    wrong_answers_served = []
    correct_on_uncorrupted = 0
    for i in range(ENTITY_COUNT):
        result = pipeline.answer(f"q{i}", f"user:{i}", "plan", NOW, now=NOW)
        expected = f"user:{i}.plan = plan-{i}"
        if i in corrupted_entities:
            if result.verified and result.claim != expected:
                wrong_answers_served.append((i, result.claim))
        elif result.verified and result.claim == expected:
            correct_on_uncorrupted += 1

    # The whole point of C3: a corrupted projection may cause abstentions,
    # but it must NEVER cause a confidently wrong VERIFIED answer.
    assert wrong_answers_served == []

    uncorrupted_count = ENTITY_COUNT - len(corrupted_entities)
    assert correct_on_uncorrupted / uncorrupted_count >= 0.90


def test_corruption_at_exactly_the_documented_30_percent_boundary_still_meets_the_90_percent_floor(make_log, make_event):
    # The existing test above corrupts 4 of 10 (40%, per its own CORRUPT_EVERY
    # comment) -- this pins down the literal boundary the spec table states
    # (30%/90%) rather than only a comfortably-inside-the-margin case.
    log = _build_log(make_log, make_event)
    events = log.read_all()

    view, pv = build_supersession_chains(events, builder="test", built_at=NOW)
    truth = build_ground_truth(events)

    corrupted_entities = {0, 1, 2}  # exactly 3 of 10 == 30%
    corrupted_keys = {f"user:{i}::plan" for i in corrupted_entities}
    view = _corrupt_chains(view, corrupted_keys)

    pipeline = Pipeline(
        view=view,
        events_by_id={e.id: e for e in events},
        projection_version=pv,
        cache=RouteCache(),
        deriver=ScriptedDeriver(),
        verifier=Heimdall(truth, truth_version="test-v1"),
        ledger=OutcomeLedger(log),
    )

    wrong_answers_served = []
    correct_on_uncorrupted = 0
    for i in range(ENTITY_COUNT):
        result = pipeline.answer(f"q{i}", f"user:{i}", "plan", NOW, now=NOW)
        expected = f"user:{i}.plan = plan-{i}"
        if i in corrupted_entities:
            if result.verified and result.claim != expected:
                wrong_answers_served.append((i, result.claim))
        elif result.verified and result.claim == expected:
            correct_on_uncorrupted += 1

    assert wrong_answers_served == []
    uncorrupted_count = ENTITY_COUNT - len(corrupted_entities)
    assert correct_on_uncorrupted / uncorrupted_count >= 0.90
