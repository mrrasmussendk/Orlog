"""Contract: L0 events cannot be mutated -- no update, no delete, no
in-place edit -- plus the v1.0 additions: id/recorded_at/prev_hash are
assigned by the log (never the caller), and the hash chain verifies.
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from orlog.errors import SchemaError


def test_append_then_read_all_round_trips(make_log, make_event):
    log = make_log()
    e1 = log.append(make_event(payload={"n": 1}))
    e2 = log.append(make_event(payload={"n": 2}))

    stored = log.read_all()
    assert [e.payload for e in stored] == [{"n": 1}, {"n": 2}]
    assert [e.id for e in stored] == [e1.id, e2.id]


def test_appending_never_touches_earlier_lines(tmp_path, make_log, make_event):
    log_path = tmp_path / "log.jsonl"
    log = make_log()

    log.append(make_event(payload={"n": 1}))
    before = log_path.read_text(encoding="utf-8")
    log.append(make_event(payload={"n": 2}))
    after = log_path.read_text(encoding="utf-8")

    assert after.startswith(before)


def test_update_is_refused(make_log):
    with pytest.raises(NotImplementedError):
        make_log().update()


def test_delete_is_refused(make_log):
    with pytest.raises(NotImplementedError):
        make_log().delete()


def test_event_object_cannot_be_mutated_in_place(make_log, make_event):
    event = make_log().append(make_event())
    with pytest.raises(ValidationError):
        event.payload = {"tampered": True}


def test_id_recorded_at_and_prev_hash_are_assigned_by_the_log_not_the_caller(make_log, make_event):
    # EventDraft (what make_event returns) has none of these fields --
    # the type system itself makes "the caller supplies them" impossible.
    draft = make_event()
    assert not hasattr(draft, "id")
    assert not hasattr(draft, "recorded_at")
    assert not hasattr(draft, "prev_hash")

    event = make_log().append(draft)
    assert event.id
    assert event.recorded_at is not None
    assert event.prev_hash == ""  # first event in an empty log


def test_hash_chain_links_successive_events(make_log, make_event):
    log = make_log()
    e1 = log.append(make_event(payload={"n": 1}))
    e2 = log.append(make_event(payload={"n": 2}))

    assert e1.prev_hash == ""
    assert e2.prev_hash != ""
    assert log.verify_chain() is True


def test_verify_chain_detects_tampering_with_the_file_on_disk(tmp_path, make_log, make_event):
    log_path = tmp_path / "log.jsonl"
    log = make_log()
    log.append(make_event(payload={"n": 1}))
    log.append(make_event(payload={"n": 2}))

    # Simulate an attacker editing the JSONL file directly -- the one thing
    # urd's own API refuses to let anyone do through its methods.
    lines = log_path.read_text(encoding="utf-8").splitlines()
    tampered_first_line = lines[0].replace('"n":1', '"n":999')
    log_path.write_text("\n".join([tampered_first_line] + lines[1:]) + "\n", encoding="utf-8")

    assert log.verify_chain() is False


def test_get_finds_an_event_by_id(make_log, make_event):
    log = make_log()
    e1 = log.append(make_event(payload={"n": 1}))
    log.append(make_event(payload={"n": 2}))

    assert log.get(e1.id) == e1
    assert log.get("nonexistent") is None


def test_read_filters_by_since_and_until(make_log, make_event):
    log = make_log()
    e1 = log.append(make_event(payload={"n": 1}))
    e2 = log.append(make_event(payload={"n": 2}))
    e3 = log.append(make_event(payload={"n": 3}))

    assert [e.id for e in log.read(since=e2.id)] == [e2.id, e3.id]
    assert [e.id for e in log.read(until=e2.id)] == [e1.id, e2.id]


def test_invalid_draft_raises_schema_error(make_log, make_event):
    # occurred_at in the far future can never satisfy "recorded_at >=
    # occurred_at" against the log's own clock -- this must be rejected at
    # append() time with E_SCHEMA.
    far_future = datetime(9999, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(SchemaError):
        make_log().append(make_event(occurred_at=far_future))
