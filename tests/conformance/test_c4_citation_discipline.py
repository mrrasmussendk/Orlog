"""Conformance cell C4: citation discipline (ORLOG-SPEC.md §6).

PASS criterion (spec table): every assertion with an uncited, hallucinated,
or expired citation fails verification with the correct code, and never
reaches the caller as a served Answer -- only ever as an Abstention.

Each adversarial Deriver below bypasses ScriptedDeriver entirely and hands
heimdall a deliberately bad Assertion directly, standing in for "the model
misbehaved" -- exactly what spec §4.4 says a real (LLM) Deriver must guard
against.
"""

from datetime import datetime, timezone

from orlog.heimdall import Heimdall, build_ground_truth
from orlog.huginn import Assertion
from orlog.muninn import RouteCache
from orlog.pipeline import Pipeline
from orlog.skuld import OutcomeLedger
from orlog.verdandi import build_supersession_chains

T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 6, 1, tzinfo=timezone.utc)


class _UncitedDeriver:
    def derive(self, query, as_of, candidates, *, query_id, derived_at, route):
        return Assertion(query_id=query_id, claim="doesn't matter", citations=[], derived_by="adversarial", derived_at=derived_at, route=route)


class _HallucinatedIdDeriver:
    def derive(self, query, as_of, candidates, *, query_id, derived_at, route):
        return Assertion(query_id=query_id, claim=f"{query} = pro", citations=["not-a-real-event-id"], derived_by="adversarial", derived_at=derived_at, route=route)


class _ExpiredCitationDeriver:
    def __init__(self, expired_event_id, value):
        self._id = expired_event_id
        self._value = value

    def derive(self, query, as_of, candidates, *, query_id, derived_at, route):
        return Assertion(query_id=query_id, claim=f"{query} = {self._value}", citations=[self._id], derived_by="adversarial", derived_at=derived_at, route=route)


def _build_pipeline(log, deriver):
    events = log.read_all()
    view, pv = build_supersession_chains(events, builder="test", built_at=T2)
    truth = build_ground_truth(events)
    pipeline = Pipeline(
        view=view,
        events_by_id={e.id: e for e in events},
        projection_version=pv,
        cache=RouteCache(),
        deriver=deriver,
        verifier=Heimdall(truth, truth_version="test-v1"),
        ledger=OutcomeLedger(log),
    )
    return pipeline, events


def test_uncited_assertion_never_reaches_the_caller_as_an_answer(make_log, make_event):
    log = make_log()
    log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "plan", "value": "free"}))
    log.append(make_event(occurred_at=T2, payload={"entity": "user:1", "attribute": "plan", "value": "pro"}))
    pipeline, _ = _build_pipeline(log, _UncitedDeriver())

    result = pipeline.answer("q1", "user:1", "plan", T2, now=T2)

    assert result.abstained is True
    assert "UNCITED" in result.reasons


def test_hallucinated_citation_never_reaches_the_caller_as_an_answer(make_log, make_event):
    log = make_log()
    log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "plan", "value": "free"}))
    log.append(make_event(occurred_at=T2, payload={"entity": "user:1", "attribute": "plan", "value": "pro"}))
    pipeline, _ = _build_pipeline(log, _HallucinatedIdDeriver())

    result = pipeline.answer("q2", "user:1", "plan", T2, now=T2)

    assert result.abstained is True
    assert "NOT_FOUND" in result.reasons


def test_expired_citation_never_reaches_the_caller_as_an_answer(make_log, make_event):
    log = make_log()
    log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "plan", "value": "free"}))
    log.append(make_event(occurred_at=T2, payload={"entity": "user:1", "attribute": "plan", "value": "pro"}))
    events = log.read_all()
    expired_event = events[0]  # the "free" fact, superseded by "pro" at T2

    pipeline, _ = _build_pipeline(log, _ExpiredCitationDeriver(expired_event.id, "free"))

    result = pipeline.answer("q3", "user:1", "plan", T2, now=T2)

    assert result.abstained is True
    assert "EXPIRED" in result.reasons
