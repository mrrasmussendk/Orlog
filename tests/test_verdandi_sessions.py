"""Contract: the session_summaries projection groups by payload.session_id,
is pure/rebuild-identical, and skips events with no session_id.
"""

from datetime import datetime, timezone

import pytest

from orlog.urd import EventLog
from orlog.verdandi_sessions import build_session_summaries

CLOCK = lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)  # noqa: E731
T1 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 1, 1, 10, 5, tzinfo=timezone.utc)
BUILT_AT = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _events(tmp_path, make_event):
    log = EventLog(tmp_path / "log.jsonl", clock=CLOCK)
    return [
        log.append(make_event(occurred_at=T1, type="utterance", payload={"session_id": "s1", "text": "hello"})),
        log.append(make_event(occurred_at=T2, type="utterance", payload={"session_id": "s1", "text": "how are you"})),
        log.append(make_event(occurred_at=T1, type="utterance", payload={"session_id": "s2", "text": "different session"})),
        log.append(make_event(occurred_at=T1, type="fact", payload={"entity": "user:1", "attribute": "plan", "value": "pro"})),  # no session_id
    ]


def test_events_are_grouped_by_session_id(tmp_path, make_event):
    events = _events(tmp_path, make_event)
    view, pv = build_session_summaries(events, builder="test", built_at=BUILT_AT)

    assert set(view.sessions.keys()) == {"s1", "s2"}
    assert view.sessions["s1"].event_count == 2
    assert view.sessions["s2"].event_count == 1
    assert pv.projection == "session_summaries"


def test_digest_is_a_deterministic_template_not_a_model_summary(tmp_path, make_event):
    events = _events(tmp_path, make_event)
    view, _ = build_session_summaries(events, builder="test", built_at=BUILT_AT)

    digest = view.sessions["s1"].digest
    assert "2 events" in digest
    assert "hello" in digest
    assert "how are you" in digest


def test_events_without_a_session_id_are_skipped_not_erroring(tmp_path, make_event):
    events = _events(tmp_path, make_event)
    view, _ = build_session_summaries(events, builder="test", built_at=BUILT_AT)

    all_session_event_ids = {eid for s in view.sessions.values() for eid in s.event_ids}
    fact_event = events[3]
    assert fact_event.id not in all_session_event_ids


def test_rebuilding_from_the_same_log_is_byte_identical(tmp_path, make_event):
    events = _events(tmp_path, make_event)

    view_a, pv_a = build_session_summaries(events, builder="test", built_at=BUILT_AT, version=1)
    view_b, pv_b = build_session_summaries(events, builder="test", built_at=BUILT_AT, version=1)

    assert view_a.model_dump_json() == view_b.model_dump_json()
    assert pv_a.model_dump_json() == pv_b.model_dump_json()


def test_empty_events_raises():
    with pytest.raises(ValueError):
        build_session_summaries([], builder="test", built_at=BUILT_AT)


def test_events_with_no_session_ids_at_all_raises(tmp_path, make_event):
    log = EventLog(tmp_path / "log.jsonl", clock=CLOCK)
    events = [log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "plan", "value": "pro"}))]

    with pytest.raises(ValueError):
        build_session_summaries(events, builder="test", built_at=BUILT_AT)
