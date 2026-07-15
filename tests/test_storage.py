"""Contract: SegmentedLog rotates and chains across segments; EventIndex is
entirely derivable from the segments (spec §B2 G2 test).
"""

from datetime import datetime, timezone

import pytest

from orlog.errors import LockedError
from orlog.storage import EventIndex, SegmentedLog
from orlog.workspace import Workspace

CLOCK = lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)  # noqa: E731
T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_segmented_log_rotates_at_the_byte_threshold(tmp_path, make_event):
    log = SegmentedLog(tmp_path / "events", clock=CLOCK, rotate_bytes=200)
    for i in range(10):
        log.append(make_event(occurred_at=T1, payload={"n": i, "padding": "x" * 50}))

    segments = sorted((tmp_path / "events").glob("log-*.jsonl"))
    assert len(segments) > 1  # forced at least one rotation
    assert len(log.read_all()) == 10  # nothing lost across the rotation


def test_hash_chain_spans_segments(tmp_path, make_event):
    log = SegmentedLog(tmp_path / "events", clock=CLOCK, rotate_bytes=200)
    for i in range(10):
        log.append(make_event(occurred_at=T1, payload={"n": i, "padding": "x" * 50}))

    assert len(sorted((tmp_path / "events").glob("log-*.jsonl"))) > 1
    assert log.verify_chain(full=True) is True


def test_event_index_is_derivable_and_matches_the_log(tmp_path, make_event):
    log = SegmentedLog(tmp_path / "events", clock=CLOCK, rotate_bytes=200)
    for i in range(5):
        log.append(make_event(occurred_at=T1, payload={"n": i}, entities=[f"PERSON_{i}"]))

    index = EventIndex(tmp_path / "index.sqlite")
    index.rebuild(log)

    rows = index._conn.execute("SELECT id FROM events_idx").fetchall()
    assert len(rows) == 5

    entity_rows = index._conn.execute("SELECT entity FROM entities_idx").fetchall()
    assert {r[0] for r in entity_rows} == {f"PERSON_{i}" for i in range(5)}

    # Rebuilding again from scratch must land on exactly the same rows --
    # the index has no state of its own beyond what the log determines.
    before = sorted(index._conn.execute("SELECT id, segment, offset FROM events_idx").fetchall())
    index.rebuild(log)
    after = sorted(index._conn.execute("SELECT id, segment, offset FROM events_idx").fetchall())
    assert before == after


def test_live_indexed_offsets_match_a_full_rebuild(tmp_path, make_event):
    """Mirrors what Runtime.remember() actually does: index_event() called
    once per event, right after its own append -- not batched at the end
    the way rebuild() is. Each one must get its OWN offset (0, 1, 2, ...),
    not a stale/wrong one, or a live-built index silently disagrees with
    what a full rebuild computes for the same log (spec §B2 G2: index.sqlite
    MUST be derivable, i.e. reproducible row-for-row from the segments).
    """
    log = SegmentedLog(tmp_path / "events", clock=CLOCK)
    live_index = EventIndex(tmp_path / "live.sqlite")
    for i in range(3):
        event = log.append(make_event(occurred_at=T1, payload={"n": i}))
        live_index.index_event(event, segment=log._current.path.name, offset=log.current_segment_offset())

    rebuilt_index = EventIndex(tmp_path / "rebuilt.sqlite")
    rebuilt_index.rebuild(log)

    live_rows = sorted(live_index._conn.execute("SELECT id, segment, offset FROM events_idx").fetchall())
    rebuilt_rows = sorted(rebuilt_index._conn.execute("SELECT id, segment, offset FROM events_idx").fetchall())
    assert live_rows == rebuilt_rows
    assert [row[2] for row in live_rows] == [0, 1, 2]


def test_workspace_scaffold_creates_the_expected_layout(tmp_path):
    ws = Workspace(tmp_path / "myproject")
    ws.scaffold()

    assert ws.events_dir.is_dir()
    assert ws.derivations_cache_dir.is_dir()


def test_workspace_lock_prevents_a_second_holder(tmp_path):
    ws1 = Workspace(tmp_path / "myproject")
    ws1.scaffold()
    ws1.acquire_lock()

    ws2 = Workspace(tmp_path / "myproject")
    with pytest.raises(LockedError):
        ws2.acquire_lock()

    ws1.release_lock()
    ws2.acquire_lock()  # now free
    ws2.release_lock()


def test_workspace_as_context_manager_releases_the_lock_on_exit(tmp_path):
    ws = Workspace(tmp_path / "myproject")
    with ws:
        assert ws.lock_path.exists()
    assert not ws.lock_path.exists()
