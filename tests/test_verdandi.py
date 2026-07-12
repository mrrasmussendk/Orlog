"""Contract: verdandi's supersession_chains build is pure -- same events and
builder in, byte-identical View and ProjectionVersion out (ORLOG-SPEC.md
§4.2 MUST clause).
"""

from datetime import datetime, timezone

import pytest

from orlog.verdandi import OPEN_VALID_TO, build_supersession_chains

T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 4, 1, tzinfo=timezone.utc)
T3 = datetime(2026, 8, 1, tzinfo=timezone.utc)
BUILT_AT = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _three_revision_events(make_log, make_event):
    # build_supersession_chains needs real Events (id/recorded_at assigned),
    # so these have to actually go through an EventLog, not just be built
    # as drafts in memory.
    log = make_log()
    return [
        log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "plan", "value": "free"})),
        log.append(make_event(occurred_at=T2, payload={"entity": "user:1", "attribute": "plan", "value": "pro"})),
        log.append(make_event(occurred_at=T3, payload={"entity": "user:1", "attribute": "plan", "value": "enterprise"})),
    ]


def test_rebuilding_from_the_same_log_is_byte_identical(make_log, make_event):
    events = _three_revision_events(make_log, make_event)

    view_a, pv_a = build_supersession_chains(events, builder="test-builder", built_at=BUILT_AT, version=1)
    view_b, pv_b = build_supersession_chains(events, builder="test-builder", built_at=BUILT_AT, version=1)

    assert view_a.model_dump_json() == view_b.model_dump_json()
    assert pv_a.model_dump_json() == pv_b.model_dump_json()


def test_chain_is_ordered_oldest_first_with_correct_windows(make_log, make_event):
    events = _three_revision_events(make_log, make_event)
    view, pv = build_supersession_chains(events, builder="test-builder", built_at=BUILT_AT)

    chain = view.chains["user:1::plan"]
    assert [events[0].id, events[1].id, events[2].id] == chain

    w0 = view.windows[events[0].id]
    w1 = view.windows[events[1].id]
    w2 = view.windows[events[2].id]
    assert (w0.valid_from, w0.valid_to) == (T1, T2)
    assert (w1.valid_from, w1.valid_to) == (T2, T3)
    assert (w2.valid_from, w2.valid_to) == (T3, OPEN_VALID_TO)

    assert pv.built_from == events[-1].id
    assert pv.projection == "supersession_chains"


def test_empty_log_raises():
    with pytest.raises(ValueError):
        build_supersession_chains([], builder="test-builder", built_at=BUILT_AT)
