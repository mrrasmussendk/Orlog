"""Contract: Pipeline.answer() records the observability counters spec §B8
lists that can't be reconstructed by replaying outcome.* events alone --
route-specific counts (cache_hits, cache_evicts, rederivations).
"""

import time
from datetime import datetime, timezone

from orlog.heimdall import Heimdall, VerificationFailure, VerificationResult, build_ground_truth
from orlog.huginn import ScriptedDeriver
from orlog.muninn import RouteCache
from orlog.observability import Stats
from orlog.pipeline import Pipeline
from orlog.skuld import OutcomeLedger
from orlog.verdandi import build_supersession_chains

T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _pipeline(log, stats):
    events = log.read_all()
    view, pv = build_supersession_chains(events, builder="test", built_at=T1)
    truth = build_ground_truth(events)
    return Pipeline(
        view=view,
        events_by_id={e.id: e for e in events},
        projection_version=pv,
        cache=RouteCache(),
        deriver=ScriptedDeriver(),
        verifier=Heimdall(truth, truth_version="v1"),
        ledger=OutcomeLedger(log),
        stats=stats,
    )


def test_recall_verification_and_cache_hit_are_all_counted(make_log, make_event):
    log = make_log()
    log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "status", "value": "active"}))
    stats = Stats()
    pipeline = _pipeline(log, stats)

    pipeline.answer("q1", "user:1", "status", T2, now=T1)
    pipeline.answer("q2", "user:1", "status", T2, now=T1)

    snapshot = stats.snapshot()
    assert snapshot["recalls"] == 2
    assert snapshot["cache_hits"] == 1
    assert snapshot["verifications"]["pass"] == 2


def test_no_candidates_is_counted_as_an_abstention(make_log, make_event):
    log = make_log()
    log.append(make_event(occurred_at=T2, payload={"entity": "user:1", "attribute": "status", "value": "active"}))
    stats = Stats()
    pipeline = _pipeline(log, stats)

    pipeline.answer("q1", "user:1", "status", T1, now=T2)  # before the fact existed

    snapshot = stats.snapshot()
    assert snapshot["abstentions"]["by_reason"] == {"NO_CANDIDATES": 1}


class _AlwaysFailVerifier:
    """Forces every verify() to fail -- used to drive Pipeline.answer() down
    its real fail-then-abstain branch (single candidate, so the pre-filter
    empties `valid` and it abstains after exactly one verify call) without
    needing to contrive a genuine ground-truth/retrieval mismatch. Exposes
    only truth_version and verify() -- the only two members answer() touches
    on this specific code path (the citation is excluded by the bad_ids
    check before would_pass_validity would run, and fact() is only reached
    on the served/pass path this scenario never takes)."""

    def __init__(self, truth_version):
        self.truth_version = truth_version

    def verify(self, assertion, as_of, *, checked_at):
        return VerificationResult(
            assertion_query_id=assertion.query_id, status="fail", checked_at=checked_at,
            truth_version=self.truth_version,
            failures=[VerificationFailure(citation=assertion.citations[0], code="UNSUPPORTED", detail="forced failure for test")],
        )


def test_a_failed_verification_is_logged_even_though_the_query_ultimately_abstains(make_log, make_event):
    log = make_log()
    log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "status", "value": "active"}))
    events = log.read_all()
    view, pv = build_supersession_chains(events, builder="test", built_at=T1)
    pipeline = Pipeline(
        view=view, events_by_id={e.id: e for e in events}, projection_version=pv,
        cache=RouteCache(), deriver=ScriptedDeriver(),
        verifier=_AlwaysFailVerifier("v1"), ledger=OutcomeLedger(log),
    )

    result = pipeline.answer("q1", "user:1", "status", T2, now=T1)

    assert result.abstained is True
    # Before the fix, a failed verification was only ever logged if the
    # query eventually passed -- an abstention logged NO outcome.verification
    # event at all, only outcome.abstention, silently losing which citation
    # failed and why.
    verification_events = [e for e in log.read_all() if e.type == "outcome.verification"]
    assert len(verification_events) == 1
    assert verification_events[0].payload["status"] == "fail"
    assert verification_events[0].payload["failures"][0]["code"] == "UNSUPPORTED"


def test_non_llm_latency_is_measured_even_with_no_llm_in_play(make_log, make_event):
    # ScriptedDeriver never sets Assertion.latency_ms (it makes no model
    # call, so there's nothing honest to report there) -- before the fix,
    # stats.p50_ms/p95_ms stayed null forever on exactly this (the default,
    # non-LLM-backed) configuration, contradicting spec §B8's observability
    # requirement. answer() must measure its own non-LLM wall-clock time
    # directly instead of relying on the deriver to self-report it.
    log = make_log()
    log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "status", "value": "active"}))
    stats = Stats()
    pipeline = _pipeline(log, stats)

    pipeline.answer("q1", "user:1", "status", T2, now=T1)

    snapshot = stats.snapshot()
    assert snapshot["p50_ms"] is not None
    assert snapshot["p95_ms"] is not None
    assert snapshot["p50_ms"] >= 0.0


def test_stats_is_optional_and_changes_no_behavior(make_log, make_event):
    log = make_log()
    log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "status", "value": "active"}))
    pipeline = _pipeline(log, stats=None)

    result = pipeline.answer("q1", "user:1", "status", T2, now=T1)

    assert result.verified is True


def test_a_self_contradictory_fact_fails_fast_without_ever_deriving(make_log, make_event):
    # payload["self_supported"] mirrors what Runtime.remember() computes at
    # write time when a fact's own remembered text doesn't support the value
    # it was recorded with (set directly here to unit-test the pipeline/
    # heimdall wiring in isolation from Runtime.remember()'s own check --
    # see tests/test_server_tools.py for the end-to-end version).
    log = make_log()
    log.append(make_event(
        occurred_at=T1,
        payload={"entity": "person:trap", "attribute": "city", "value": "Tokyo", "text": "moved to Paris", "self_supported": False},
    ))
    stats = Stats()
    pipeline = _pipeline(log, stats)

    result = pipeline.answer("q1", "person:trap", "city", T2, now=T1)

    assert result.abstained is True
    assert result.reasons == ["UNSUPPORTED_BY_SOURCE"]
    assert stats.snapshot()["verifications"]["fail"] == {"UNSUPPORTED_BY_SOURCE": 1}


class _HangingDeriver:
    """Simulates a deriver that never returns (e.g. a stalled network call)
    -- used to prove Pipeline.answer()'s own timeout is a real backstop, not
    just documentation, independent of whatever timeout the deriver itself
    claims to enforce.
    """

    def derive(self, *args, **kwargs):
        time.sleep(60)


def test_a_hanging_derive_times_out_instead_of_blocking_the_read(make_log, make_event):
    log = make_log()
    log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "status", "value": "active"}))
    events = log.read_all()
    view, pv = build_supersession_chains(events, builder="test", built_at=T1)
    pipeline = Pipeline(
        view=view, events_by_id={e.id: e for e in events}, projection_version=pv,
        cache=RouteCache(), deriver=_HangingDeriver(),
        verifier=Heimdall(build_ground_truth(events), truth_version="v1"), ledger=OutcomeLedger(log),
        answer_timeout_s=0.2,
    )

    start = time.monotonic()
    result = pipeline.answer("q1", "user:1", "status", T2, now=T1)
    elapsed = time.monotonic() - start

    assert elapsed < 5  # bounded by answer_timeout_s, not the 60s fake hang
    assert result.abstained is True
    assert result.reasons == ["VERIFY_TIMEOUT"]
