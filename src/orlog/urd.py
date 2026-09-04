"""urd: the L0 event log. The fixed past -- append-only, hash-chained, never
rewritten (ORLOG-SPEC.md §A1/§A3/§4.1).

Design decisions:

1. Storage is one JSON object per line (JSONL). append() is a single
   `open(..., "a")` write -- there is no way to accidentally open the file
   in a mode that would let you overwrite an earlier line. (Segment
   rotation at 64MB, per spec §B2, lives in storage.py, not here -- this
   class is the single-segment primitive it rotates between.)

2. `append()` takes an EventDraft (occurred_at/actor/type/payload/entities/
   supersedes_hint) and returns a full Event. `id`, `recorded_at`, and
   `prev_hash` are assigned HERE, never by the caller -- spec §A3 is
   explicit about this ("MUST: recorded_at and prev_hash assigned by the
   log"). `recorded_at` comes from an injectable `clock` callable
   (defaulting to real UTC now) so tests can supply a deterministic clock
   without this class ever calling datetime.now() by default in a way
   tests can't control.

3. `update()`/`delete()` exist as named methods that raise, rather than
   simply not existing -- an explicit, discoverable, testable "no update,
   no delete" API (see tests/test_urd.py) instead of an implicit gap.

4. The hash chain: prev_hash of event N is sha256("sha256:" prefix) of
   event N-1's full canonical JSON (sorted keys, no whitespace, UTF-8 --
   spec §A3), INCLUDING event N-1's own prev_hash. That's what makes it a
   chain: tampering with any earlier event changes every hash after it.
   verify_chain() recomputes the whole thing from scratch.

5. `initial_prev_hash` exists so storage.py's segmented log (§B2) can chain
   a brand new segment file from the previous segment's last event -- the
   hash chain is meant to span the whole workspace, not restart at zero
   every time a segment rotates. It's ignored once the file already has
   its own events (their own prev_hash then rules, as normal).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, NoReturn

from pydantic import ValidationError
from ulid import ULID

from orlog.errors import SchemaError
from orlog.models.event import Event, EventDraft


def _canonical_json(data: dict) -> str:
    """spec §A3: 'Canonical serialization for hashing: JSON with sorted
    keys, no whitespace, UTF-8.'"""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash_of(event: Event) -> str:
    canonical = _canonical_json(json.loads(event.model_dump_json()))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


class EventLog:
    """An append-only, hash-chained JSONL log of Events."""

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
        initial_prev_hash: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.touch(exist_ok=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._last_hash = self._compute_last_hash(initial_prev_hash)

    def _compute_last_hash(self, initial_prev_hash: str | None) -> str:
        events = self.read_all()
        if events:
            return _hash_of(events[-1])
        return initial_prev_hash if initial_prev_hash is not None else ""

    def append(self, draft: EventDraft, *, recorded_at: datetime | None = None) -> Event:
        """Validate, assign id/recorded_at/prev_hash, and write one Event.

        `recorded_at` overrides the log's clock. It exists for BACKFILL and
        IMPORT -- replaying a historical record whose transaction times are
        already known -- and is deliberately a keyword-only argument to
        append() rather than a field on EventDraft: spec §A3's "assigned by
        the log, never the caller" stays true of the draft an ordinary
        caller builds, so no ordinary write can set it by accident. Event's
        own recorded_at >= occurred_at validator still applies.

        Note that backfilling breaks the incidental invariant that
        recorded_at rises with append order, so anything filtering by
        recorded_at must filter, not slice a prefix -- see
        Runtime.build_pipeline()'s known_as_of.
        """
        try:
            event = Event(
                id=str(ULID()),
                occurred_at=draft.occurred_at,
                recorded_at=self._clock() if recorded_at is None else recorded_at,
                actor=draft.actor,
                type=draft.type,
                payload=draft.payload,
                entities=draft.entities,
                supersedes_hint=draft.supersedes_hint,
                prev_hash=self._last_hash,
            )
        except ValidationError as exc:
            raise SchemaError(str(exc)) from exc

        with self.path.open("a", encoding="utf-8") as f:
            f.write(event.model_dump_json() + "\n")
        self._last_hash = _hash_of(event)
        return event

    def read_all(self) -> list[Event]:
        """Return every Event ever appended, in append order."""
        text = self.path.read_text(encoding="utf-8")
        events = []
        for line in text.splitlines():
            if not line.strip():
                continue
            events.append(Event.model_validate(json.loads(line)))
        return events

    def read(self, since: str | None = None, until: str | None = None) -> Iterator[Event]:
        """Yield events with id in [since, until] (inclusive). ULIDs sort
        lexicographically by creation time, so string comparison is enough
        -- no separate index needed for this reference implementation.
        """
        for event in self.read_all():
            if since is not None and event.id < since:
                continue
            if until is not None and event.id > until:
                continue
            yield event

    def get(self, event_id: str) -> Event | None:
        for event in self.read_all():
            if event.id == event_id:
                return event
        return None

    def verify_chain(self, *, expected_prev: str = "") -> bool:
        """Recompute every event's hash and confirm the chain is intact.

        `expected_prev` lets a caller verify a segment that isn't the first
        in a workspace (storage.SegmentedLog's newest-segment startup
        check) -- its first event's prev_hash should equal the previous
        segment's last hash, not "".
        """
        for event in self.read_all():
            if event.prev_hash != expected_prev:
                return False
            expected_prev = _hash_of(event)
        return True

    def update(self, *_args, **_kwargs) -> NoReturn:
        """Refuses. urd is append-only: ground truth is never edited in place."""
        raise NotImplementedError(
            "EventLog is append-only: events cannot be updated. "
            "If a fact changed, append a new event describing the change "
            "(see DESIGN-PRINCIPLES.md principle 1)."
        )

    def delete(self, *_args, **_kwargs) -> NoReturn:
        """Refuses. urd is append-only: ground truth is never removed."""
        raise NotImplementedError(
            "EventLog is append-only: events cannot be deleted. "
            "See DESIGN-PRINCIPLES.md principle 1."
        )
