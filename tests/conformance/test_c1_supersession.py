"""Conformance cell C1: supersession (ORLOG-SPEC.md §6).

PASS criterion (spec table): 100% of answerable as-of questions are answered
from the version valid at that as_of; 0 answers derived from destroyed or
overwritten history.
"""

from datetime import datetime, timezone

from orlog.heimdall import Heimdall, build_ground_truth
from orlog.huginn import ScriptedDeriver
from orlog.muninn import RouteCache
from orlog.pipeline import Pipeline
from orlog.skuld import OutcomeLedger
from orlog.verdandi import build_supersession_chains

T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 4, 1, tzinfo=timezone.utc)
T3 = datetime(2026, 8, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _build_pipeline(log):
    events = log.read_all()
    view, pv = build_supersession_chains(events, builder="test", built_at=NOW)
    truth = build_ground_truth(events)
    return Pipeline(
        view=view,
        events_by_id={e.id: e for e in events},
        projection_version=pv,
        cache=RouteCache(),
        deriver=ScriptedDeriver(),
        verifier=Heimdall(truth, truth_version="test-v1"),
        ledger=OutcomeLedger(log),
    )


def test_every_as_of_question_is_answered_from_the_version_valid_then(make_log, make_event):
    log = make_log()
    log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "plan", "value": "free"}))
    log.append(make_event(occurred_at=T2, payload={"entity": "user:1", "attribute": "plan", "value": "pro"}))
    log.append(make_event(occurred_at=T3, payload={"entity": "user:1", "attribute": "plan", "value": "enterprise"}))

    pipeline = _build_pipeline(log)

    # Across every revision boundary, as-of must resolve to the version that
    # was actually true at that moment -- never a version that came before
    # or after it in the chain.
    expected = [(T1, "free"), (T2, "pro"), (T3, "enterprise"), (NOW, "enterprise")]
    for i, (as_of, expected_value) in enumerate(expected):
        result = pipeline.answer(f"q{i}", "user:1", "plan", as_of, now=NOW)
        assert result.verified is True, f"as_of={as_of} unexpectedly abstained: {result}"
        assert result.claim == f"user:1.plan = {expected_value}"


def test_a_question_before_any_revision_existed_is_correctly_unanswerable(make_log, make_event):
    log = make_log()
    log.append(make_event(occurred_at=T2, payload={"entity": "user:1", "attribute": "plan", "value": "pro"}))

    pipeline = _build_pipeline(log)
    result = pipeline.answer("q-before", "user:1", "plan", T1, now=NOW)

    assert result.abstained is True
    assert "NO_CANDIDATES" in result.reasons


def test_no_answer_is_ever_derived_from_a_superseded_version(make_log, make_event):
    log = make_log()
    log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "plan", "value": "free"}))
    log.append(make_event(occurred_at=T2, payload={"entity": "user:1", "attribute": "plan", "value": "pro"}))

    pipeline = _build_pipeline(log)

    # Querying "now" (well after both revisions) must never surface "free" --
    # that history exists in the log (nothing was destroyed) but is not the
    # answer to a current-state question.
    result = pipeline.answer("q-now", "user:1", "plan", NOW, now=NOW)
    assert result.verified is True
    assert "free" not in result.claim
    assert result.claim == "user:1.plan = pro"


def test_two_facts_at_the_exact_same_instant_resolve_deterministically(make_log, make_event):
    # verdandi's sort key is (occurred_at, recorded_at, id) -- with the
    # fixed test clock every make_log() event shares the same recorded_at
    # too, so ties resolve by id (insertion order, for monotonically
    # generated ULIDs). The FIRST fact's validity window collapses to
    # zero-width [T1, T1) and becomes permanently unreachable; the SECOND
    # wins at exactly T1, deterministically -- not by insertion-order luck.
    log = make_log()
    log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "plan", "value": "first"}))
    log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "plan", "value": "second"}))

    pipeline = _build_pipeline(log)

    result = pipeline.answer("q-tie", "user:1", "plan", T1, now=NOW)
    assert result.verified is True
    assert result.claim == "user:1.plan = second"
