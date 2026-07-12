"""Contract: Pipeline.answer() records the observability counters spec §B8
lists that can't be reconstructed by replaying outcome.* events alone --
route-specific counts (cache_hits, cache_evicts, rederivations).
"""

from datetime import datetime, timezone

from orlog.heimdall import Heimdall, build_ground_truth
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


def test_stats_is_optional_and_changes_no_behavior(make_log, make_event):
    log = make_log()
    log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "status", "value": "active"}))
    pipeline = _pipeline(log, stats=None)

    result = pipeline.answer("q1", "user:1", "status", T2, now=T1)

    assert result.verified is True
