"""storage: B2 -- segmented JSONL log + the SQLite index rebuilt from it.

Design decisions:

1. `SegmentedLog` wraps multiple single-file `EventLog`s (urd.py), rotating
   to a new file once the current one exceeds `rotate_bytes` (default 64MB,
   spec §B2). The hash chain spans segments: a new segment's first event
   still chains from the previous segment's last event, via EventLog's
   `initial_prev_hash` constructor argument -- the chain is a property of
   the whole workspace, not of any one file.

2. `EventIndex` (index.sqlite) is a pure derived cache: every table it
   defines can be dropped and rebuilt from the segments at any time (spec
   §B2 MUST: "index.sqlite is entirely derivable"). Nothing here is ever
   ground truth -- `rebuild()` proves that by doing exactly that: drop,
   then recompute from scratch.

3. `verify_chain(full=True)` (used by `orlog replay`) independently re-walks
   every segment, tracking the running expected_prev by hand -- it does NOT
   delegate to each segment's own `EventLog.verify_chain()` call, because
   only the *first* segment's first event should have prev_hash == "";
   every later segment's first event should chain from the one before it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable

from orlog.errors import StorageError
from orlog.models.event import Event, EventDraft
from orlog.urd import EventLog, _hash_of

SEGMENT_ROTATE_BYTES = 64 * 1024 * 1024  # 64 MB, spec §B2

_DDL = """
CREATE TABLE IF NOT EXISTS events_idx (
    id TEXT PRIMARY KEY, occurred_at TEXT, recorded_at TEXT,
    actor TEXT, type TEXT, segment TEXT, offset INTEGER
);
CREATE INDEX IF NOT EXISTS ix_ev_time ON events_idx(occurred_at);
CREATE INDEX IF NOT EXISTS ix_ev_type ON events_idx(type);
CREATE TABLE IF NOT EXISTS entities_idx (event_id TEXT, entity TEXT);
CREATE TABLE IF NOT EXISTS windows (
    projection TEXT, version INTEGER, fact_id TEXT,
    valid_from TEXT, valid_to TEXT, chain TEXT,
    PRIMARY KEY (projection, version, fact_id)
);
CREATE TABLE IF NOT EXISTS embeddings (event_id TEXT PRIMARY KEY, model TEXT, dim INTEGER, vec BLOB);
CREATE TABLE IF NOT EXISTS importance (event_id TEXT PRIMARY KEY, weight REAL DEFAULT 1.0, updated_at TEXT);
CREATE TABLE IF NOT EXISTS route_cache (
    key TEXT PRIMARY KEY, assertion_json TEXT,
    created_at TEXT, last_verified_at TEXT, hits INTEGER
);
CREATE TABLE IF NOT EXISTS projections (
    projection TEXT, version INTEGER, built_from TEXT,
    built_at TEXT, builder TEXT, quality REAL,
    PRIMARY KEY (projection, version)
);
"""


class EventIndex:
    """SQLite index over a SegmentedLog. Entirely derivable -- see module
    docstring point 2. Only events_idx/entities_idx are populated by
    index_event()/rebuild() here; windows/embeddings/importance/route_cache/
    projections are written by their owning layers (verdandi, retrieval's
    embedder, skuld, muninn) -- this class just creates their tables.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.executescript(_DDL)
        self._conn.commit()

    def index_event(self, event: Event, *, segment: str, offset: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO events_idx (id, occurred_at, recorded_at, actor, type, segment, offset) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event.id, event.occurred_at.isoformat(), event.recorded_at.isoformat(), event.actor, event.type, segment, offset),
        )
        self._conn.execute("DELETE FROM entities_idx WHERE event_id = ?", (event.id,))
        for entity in event.entities:
            self._conn.execute("INSERT INTO entities_idx (event_id, entity) VALUES (?, ?)", (event.id, entity))
        self._conn.commit()

    def rebuild(self, log: "SegmentedLog") -> None:
        """Drop and rebuild events_idx/entities_idx from the segments --
        the G2 test: index.sqlite must be reproducible byte-for-byte in
        content (row-for-row; SQLite's own file bytes are not compared).
        """
        self._conn.executescript("DELETE FROM events_idx; DELETE FROM entities_idx;")
        self._conn.commit()
        for event, segment, offset in log.iter_with_location():
            self.index_event(event, segment=segment, offset=offset)

    def close(self) -> None:
        self._conn.close()


class SegmentedLog:
    """Multiple EventLog segments under `events_dir`, rotating at
    `rotate_bytes`. Presents the same read/append/verify_chain shape as a
    single EventLog, spanning however many segment files exist.
    """

    def __init__(
        self,
        events_dir: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
        rotate_bytes: int = SEGMENT_ROTATE_BYTES,
    ) -> None:
        self.events_dir = Path(events_dir)
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._rotate_bytes = rotate_bytes
        self._head_path = self.events_dir / "HEAD"
        self._segment_paths = sorted(self.events_dir.glob("log-*.jsonl"))
        if not self._segment_paths:
            self._segment_paths = [self.events_dir / "log-00001.jsonl"]
        self._current = EventLog(
            self._segment_paths[-1], clock=self._clock, initial_prev_hash=self._hash_before_current()
        )
        # Running count of events in the current segment, so
        # current_segment_offset() is O(1) per append instead of re-reading
        # and re-parsing the whole segment every time -- computed once here
        # (and again on rotation below), never inside append() itself.
        self._current_segment_count = len(self._current.read_all())

    def _next_segment_path(self) -> Path:
        """The next unused log-NNNNN.jsonl.

        Numbered from the highest segment number that actually exists, not
        from len(self._segment_paths): those diverge the moment there is a
        gap in the numbering (an archived or manually removed early
        segment), and the count-based name then collided with a segment
        already on disk. The collision appended a duplicate path to
        _segment_paths, so read_all() returned that segment's events twice
        and verify_chain(full=True) reported a break in an untampered log.
        """
        highest = 0
        for path in self.events_dir.glob("log-*.jsonl"):
            try:
                highest = max(highest, int(path.stem.split("-")[1]))
            except (IndexError, ValueError):
                continue  # not one of ours; leave it alone
        candidate = self.events_dir / f"log-{highest + 1:05d}.jsonl"
        if candidate.exists():  # belt and braces -- never rotate onto live data
            raise StorageError(f"refusing to rotate onto an existing segment: {candidate}")
        return candidate

    def _hash_before_current(self) -> str | None:
        if len(self._segment_paths) <= 1:
            return None
        prior_events = EventLog(self._segment_paths[-2], clock=self._clock).read_all()
        return _hash_of(prior_events[-1]) if prior_events else ""

    def append(self, draft: EventDraft, *, recorded_at: datetime | None = None) -> Event:
        if self._current.path.exists() and self._current.path.stat().st_size >= self._rotate_bytes:
            last_hash = self._current._last_hash
            new_path = self._next_segment_path()
            self._segment_paths.append(new_path)
            self._current = EventLog(new_path, clock=self._clock, initial_prev_hash=last_hash)
            self._current_segment_count = 0
        event = self._current.append(draft, recorded_at=recorded_at)
        self._current_segment_count += 1
        self._write_head(event)
        return event

    def _write_head(self, event: Event) -> None:
        """Record the chain head outside the segments themselves.

        verify_chain() only ever checked that each event links to the one
        before it, which makes it blind in one direction: lopping events off
        the END of the log leaves a shorter chain that is still perfectly
        self-consistent. Deleting the newest segment and the last line of
        the one before it passed verify_chain(full=True) and reported
        "hash chain verified" -- an append-only log silently losing its most
        recent memories, which is the one thing it exists not to do.

        An anchor written outside the chain closes that: the head hash and
        the total event count cannot both be reproduced by truncation.
        Written after the event is durable, so a crash between the two
        leaves the anchor BEHIND the log (detected as a mismatch and
        repairable by replay) rather than ahead of it.
        """
        self._head_path.write_text(
            json.dumps({"count": self.event_count(), "head": _hash_of(event)}),
            encoding="utf-8",
        )

    def event_count(self) -> int:
        return sum(len(EventLog(p, clock=self._clock).read_all()) for p in self._segment_paths)

    def _read_head(self) -> dict | None:
        if not self._head_path.exists():
            return None
        try:
            return json.loads(self._head_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def read_all(self) -> list[Event]:
        events: list[Event] = []
        for path in self._segment_paths:
            events.extend(EventLog(path, clock=self._clock).read_all())
        return events

    def current_segment_offset(self) -> int:
        """0-indexed line offset of the most recently appended event within
        the current segment -- matches iter_with_location()'s numbering, so
        a live index_event() call stays consistent with a full rebuild()."""
        return self._current_segment_count - 1

    def get(self, event_id: str) -> Event | None:
        for path in reversed(self._segment_paths):  # newest segment first: recent lookups are the common case
            event = EventLog(path, clock=self._clock).get(event_id)
            if event is not None:
                return event
        return None

    def iter_with_location(self):
        """Yield (event, segment_name, line_offset) for every event, in
        order -- what EventIndex.rebuild() needs to populate events_idx.
        """
        for path in self._segment_paths:
            for offset, event in enumerate(EventLog(path, clock=self._clock).read_all()):
                yield event, path.name, offset

    def verify_chain(self, *, full: bool = False) -> bool:
        """full=False (startup, spec §B2): verify only the newest segment.
        full=True (`orlog replay`): verify every segment, chained together.
        """
        if not full:
            return self._current.verify_chain(expected_prev=self._hash_before_current() or "")

        expected_prev = ""
        count = 0
        for path in self._segment_paths:
            for event in EventLog(path, clock=self._clock).read_all():
                if event.prev_hash != expected_prev:
                    return False
                expected_prev = _hash_of(event)
                count += 1

        # Forward links alone cannot detect a truncated tail -- see
        # _write_head. A workspace written before the anchor existed has no
        # HEAD file; it stays verifiable on links alone rather than being
        # reported as corrupt.
        head = self._read_head()
        if head is not None:
            if head.get("count") != count or head.get("head") != expected_prev:
                return False
        return True
