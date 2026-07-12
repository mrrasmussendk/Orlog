"""Contract: hybrid_retrieve() filters by validity BEFORE ranking, reports
honest exclusions, ranks by the spec §B3 blended score, and tie-breaks
deterministically by event id.
"""

from datetime import datetime, timezone

from orlog.retrieval_hybrid import HashingEmbedder, hybrid_retrieve
from orlog.urd import EventLog
from orlog.verdandi import build_supersession_chains

CLOCK = lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)  # noqa: E731
T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
BUILT_AT = datetime(2026, 6, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _build(tmp_path, make_event, facts):
    log = EventLog(tmp_path / "log.jsonl", clock=CLOCK)
    events = [log.append(make_event(occurred_at=T1, payload=p)) for p in facts]
    view, _ = build_supersession_chains(events, builder="test", built_at=BUILT_AT)
    events_by_id = {e.id: e for e in events}
    return view, events_by_id


def test_a_lexically_matching_fact_outranks_an_unrelated_one(tmp_path, make_event):
    view, events_by_id = _build(
        tmp_path, make_event,
        [
            {"entity": "user:1", "attribute": "plan", "value": "enterprise subscription"},
            {"entity": "user:2", "attribute": "color", "value": "the sky is blue today"},
        ],
    )

    result = hybrid_retrieve("enterprise subscription plan", NOW, view, events_by_id, HashingEmbedder())

    assert result.candidates[0].content == "enterprise subscription"


def test_facts_outside_the_validity_window_are_excluded_and_reported(tmp_path, make_event):
    log = EventLog(tmp_path / "log.jsonl", clock=CLOCK)
    e1 = log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "plan", "value": "free"}))
    e2 = log.append(make_event(occurred_at=datetime(2026, 3, 1, tzinfo=timezone.utc), payload={"entity": "user:1", "attribute": "plan", "value": "pro"}))
    view, _ = build_supersession_chains([e1, e2], builder="test", built_at=BUILT_AT)
    events_by_id = {e1.id: e1, e2.id: e2}

    # as_of T1: only "free" is valid; "pro" hasn't happened yet.
    result = hybrid_retrieve("plan", T1, view, events_by_id, HashingEmbedder(), k=10)

    assert [c.content for c in result.candidates] == ["free"]
    assert result.excluded["by_validity"] == 1


def test_k_limits_results_and_reports_the_excluded_count(tmp_path, make_event):
    view, events_by_id = _build(
        tmp_path, make_event,
        [{"entity": f"user:{i}", "attribute": "plan", "value": f"plan value {i}"} for i in range(5)],
    )

    result = hybrid_retrieve("plan value", NOW, view, events_by_id, HashingEmbedder(), k=2)

    assert len(result.candidates) == 2
    assert result.excluded["by_k"] == 3


def test_ties_break_deterministically_by_event_id(tmp_path, make_event):
    # Two facts with content that scores identically (empty query -> every
    # score is 0.0) must still come back in a stable, deterministic order.
    view, events_by_id = _build(
        tmp_path, make_event,
        [
            {"entity": "user:1", "attribute": "plan", "value": "same"},
            {"entity": "user:2", "attribute": "plan", "value": "same"},
        ],
    )

    result_a = hybrid_retrieve("", NOW, view, events_by_id, HashingEmbedder(), k=10)
    result_b = hybrid_retrieve("", NOW, view, events_by_id, HashingEmbedder(), k=10)

    ids_a = [c.event_id for c in result_a.candidates]
    ids_b = [c.event_id for c in result_b.candidates]
    assert ids_a == ids_b == sorted(ids_a)


def test_importance_weighting_can_change_the_ranking(tmp_path, make_event):
    view, events_by_id = _build(
        tmp_path, make_event,
        [
            {"entity": "user:1", "attribute": "plan", "value": "same content here"},
            {"entity": "user:2", "attribute": "plan", "value": "same content here"},
        ],
    )
    low_id = list(view.chains["user:1::plan"])[0]
    high_id = list(view.chains["user:2::plan"])[0]

    result = hybrid_retrieve(
        "same content here", NOW, view, events_by_id, HashingEmbedder(),
        importance={low_id: 0.1, high_id: 5.0},
    )

    assert result.candidates[0].event_id == high_id


def test_no_candidates_in_window_returns_empty_with_honest_exclusions(tmp_path, make_event):
    view, events_by_id = _build(
        tmp_path, make_event,
        [{"entity": "user:1", "attribute": "plan", "value": "free"}],
    )

    result = hybrid_retrieve("plan", datetime(2020, 1, 1, tzinfo=timezone.utc), view, events_by_id, HashingEmbedder())

    assert result.candidates == []
    assert result.excluded["by_validity"] == 1
