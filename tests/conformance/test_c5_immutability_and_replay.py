"""Conformance cell C5: immutability & replay (ORLOG-SPEC.md §6).

PASS criterion (spec table): no mutation path exists; hash chain verifies;
two rebuilds are byte-identical.
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from orlog.storage import SegmentedLog
from orlog.models.event import EventDraft
from orlog.verdandi import build_supersession_chains

T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 6, 1, tzinfo=timezone.utc)
BUILT_AT = datetime(2026, 7, 1, tzinfo=timezone.utc)


def test_no_mutation_path_exists_via_any_public_api(make_log, make_event):
    log = make_log()
    event = log.append(make_event(payload={"entity": "user:1", "attribute": "plan", "value": "free"}))

    # No update/delete API on the log itself...
    with pytest.raises(NotImplementedError):
        log.update()
    with pytest.raises(NotImplementedError):
        log.delete()

    # ...and no way to mutate the returned Event object in place, either.
    with pytest.raises(ValidationError):
        event.payload = {"tampered": True}


def test_hash_chain_verifies_and_detects_tampering(tmp_path, make_log, make_event):
    log_path = tmp_path / "log.jsonl"
    log = make_log()
    log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "plan", "value": "free"}))
    log.append(make_event(occurred_at=T2, payload={"entity": "user:1", "attribute": "plan", "value": "pro"}))

    assert log.verify_chain() is True

    # Editing the file directly (bypassing every public API) is the only
    # way ground truth could ever be altered -- and even that breaks the
    # chain, so it's detectable.
    lines = log_path.read_text(encoding="utf-8").splitlines()
    tampered_first_line = lines[0].replace('"free"', '"enterprise"')
    log_path.write_text("\n".join([tampered_first_line] + lines[1:]) + "\n", encoding="utf-8")

    assert log.verify_chain() is False


def test_two_rebuilds_of_the_projection_from_the_same_log_are_byte_identical(make_log, make_event):
    log = make_log()
    log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "plan", "value": "free"}))
    log.append(make_event(occurred_at=T2, payload={"entity": "user:1", "attribute": "plan", "value": "pro"}))

    events = log.read_all()
    view_a, pv_a = build_supersession_chains(events, builder="test", built_at=BUILT_AT, version=1)
    view_b, pv_b = build_supersession_chains(events, builder="test", built_at=BUILT_AT, version=1)

    assert view_a.model_dump_json() == view_b.model_dump_json()
    assert pv_a.model_dump_json() == pv_b.model_dump_json()


def test_rebuilding_after_reading_the_log_a_second_time_is_still_identical(make_log, make_event):
    # Distinct from the test above: this rebuilds from TWO SEPARATE calls to
    # read_all() (i.e. two separate reads off disk), not the same in-memory
    # `events` list reused twice -- proving replay-from-storage is pure too.
    log = make_log()
    log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "plan", "value": "free"}))
    log.append(make_event(occurred_at=T2, payload={"entity": "user:1", "attribute": "plan", "value": "pro"}))

    view_a, pv_a = build_supersession_chains(log.read_all(), builder="test", built_at=BUILT_AT, version=1)
    view_b, pv_b = build_supersession_chains(log.read_all(), builder="test", built_at=BUILT_AT, version=1)

    assert view_a.model_dump_json() == view_b.model_dump_json()
    assert pv_a.model_dump_json() == pv_b.model_dump_json()


def test_replay_is_still_byte_identical_across_a_segment_rotation_boundary(tmp_path):
    # Every other C5 test in this file uses a single-segment EventLog (via
    # make_log). This proves replay determinism survives an ACTUAL
    # multi-segment log -- rotate_bytes=200 matches the convention already
    # used in tests/test_storage.py to force rotation without writing 64MB.
    clock = lambda: BUILT_AT  # noqa: E731
    log = SegmentedLog(tmp_path / "events", clock=clock, rotate_bytes=200)
    log.append(EventDraft(occurred_at=T1, actor="test", type="fact", payload={"entity": "user:1", "attribute": "plan", "value": "free", "padding": "x" * 80}))
    log.append(EventDraft(occurred_at=T2, actor="test", type="fact", payload={"entity": "user:1", "attribute": "plan", "value": "pro", "padding": "x" * 80}))

    segments = sorted((tmp_path / "events").glob("log-*.jsonl"))
    assert len(segments) > 1  # rotation actually happened
    assert log.verify_chain(full=True) is True

    view_a, pv_a = build_supersession_chains(log.read_all(), builder="test", built_at=BUILT_AT, version=1)
    view_b, pv_b = build_supersession_chains(log.read_all(), builder="test", built_at=BUILT_AT, version=1)

    assert view_a.model_dump_json() == view_b.model_dump_json()
    assert pv_a.model_dump_json() == pv_b.model_dump_json()
