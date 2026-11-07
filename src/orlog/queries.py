"""queries: the read-only query surface over index.sqlite (ORLOG-SPEC.md §B12.4).

Before this module, every read in the server walked the whole log:
recall_history, list_entities, list_attributes and check_action each called
`log.read_all()` and filtered in Python. That is O(whole workspace) per
tool call, and at the 100k-event scale §C2 already asks `orlog replay` to
handle, "enumerate what you remember" cost seconds. The index was right
there, derived and maintained, and nothing read from it.

This module is that read side. storage.EventIndex owns writing the index;
IndexQuery owns reading it, and nothing else in the package issues SQL.

Design decisions:

1. Every query returns real `Event` objects, reassembled from
   event_bodies' canonical JSON -- not a lighter row type. Downstream
   (verdandi, heimdall, pipeline) is typed on Event and has invariants
   about it; handing those layers a near-Event with the same field names
   would be exactly the kind of "plausible but not the same thing" this
   codebase refuses elsewhere. The index stores enough to rebuild the
   event exactly, and `events_match_log()` proves it.

2. The index is a derived cache, so a query never treats it as ground
   truth. Runtime compares its coverage against the log at startup and
   rebuilds on a mismatch -- which is also what silently migrates a
   workspace written before these tables existed.

3. Time filters compare the normalized UTC columns (storage.utc_key), never
   the as-written ones. `known_as_of` is a transaction-time horizon --
   `recorded_utc <= horizon` -- and is deliberately a filter rather than a
   prefix slice, because recorded_at need not rise with append order
   (backfill, §A3), exactly as Runtime.build_pipeline documented before
   this module existed.

4. Ordering is always (segment, offset): the log's own append order. Every
   caller depends on it -- `built_from` is "the last event this build
   included", and a projection built from the same events in a different
   order is a different projection version.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Sequence

from orlog.models.event import Event
from orlog.storage import EventIndex, utc_key

_ORDER = ' ORDER BY b.segment, b."offset"'


@dataclass(frozen=True)
class EntitySummary:
    """One row of list_entities' answer, computed by SQL rather than by
    grouping the whole log in Python."""

    entity_label: str
    entity_detail: str | None
    attribute_count: int


@dataclass(frozen=True)
class OutcomeCounts:
    failures: int
    successes: int


class IndexQuery:
    """Read-only queries over an EventIndex (§B12.4)."""

    def __init__(self, index: EventIndex) -> None:
        self._index = index

    @property
    def _conn(self):
        return self._index.connection

    # -- events -----------------------------------------------------------

    def events(
        self,
        *,
        types: Iterable[str] | None = None,
        type_prefix: str | None = None,
        entity_prefix: str | None = None,
        occurred_since: datetime | None = None,
        occurred_until: datetime | None = None,
        known_as_of: datetime | None = None,
        limit: int | None = None,
    ) -> list[Event]:
        """Events matching every supplied filter, in log order.

        `types` is an exact set, `type_prefix` a namespace ("x.mimir."),
        `entity_prefix` matches either a fact's entity key or any entity
        listed on the event itself, the two `occurred_*` bounds are a
        valid-time range (half-open: since <= occurred < until, matching
        §3.3's window convention), and `known_as_of` is the transaction-time
        horizon.
        """
        where: list[str] = []
        params: list[Any] = []
        if types is not None:
            names = list(types)
            if not names:
                return []
            where.append(f"b.type IN ({','.join('?' * len(names))})")
            params.extend(names)
        if type_prefix is not None:
            where.append(_prefix_sql("b.type"))
            params.extend(_prefix_params(type_prefix))
        if entity_prefix is not None:
            where.append(
                "(EXISTS (SELECT 1 FROM facts_idx f WHERE f.event_id = b.event_id "
                f"AND ({_prefix_sql('f.entity')} OR {_prefix_sql('f.entity_label')})) "
                "OR EXISTS (SELECT 1 FROM entities_idx x WHERE x.event_id = b.event_id "
                f"AND {_prefix_sql('x.entity')}))"
            )
            params.extend(_prefix_params(entity_prefix) * 3)
        if occurred_since is not None:
            where.append("b.occurred_utc >= ?")
            params.append(utc_key(occurred_since))
        if occurred_until is not None:
            where.append("b.occurred_utc < ?")
            params.append(utc_key(occurred_until))
        if known_as_of is not None:
            where.append("b.recorded_utc <= ?")
            params.append(utc_key(known_as_of))

        sql = "SELECT b.body_json FROM event_bodies b"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += _ORDER
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [Event.model_validate_json(row[0]) for row in self._conn.execute(sql, params)]

    def fact_events(
        self,
        *,
        entity: str | None = None,
        entity_label: str | None = None,
        attribute: str | None = None,
        known_as_of: datetime | None = None,
    ) -> list[Event]:
        """The type="fact" events carrying both an entity and an attribute
        -- the exact set every projection and the pipeline are built from
        -- optionally narrowed to one key, in log order."""
        where = ["f.event_id = b.event_id"]
        params: list[Any] = []
        if entity is not None:
            where.append("f.entity = ?")
            params.append(entity)
        if entity_label is not None:
            where.append("f.entity_label = ?")
            params.append(entity_label)
        if attribute is not None:
            where.append("f.attribute = ?")
            params.append(attribute)
        if known_as_of is not None:
            where.append("f.recorded_utc <= ?")
            params.append(utc_key(known_as_of))
        sql = (
            "SELECT b.body_json FROM event_bodies b JOIN facts_idx f ON "
            + " AND ".join(where)
            + _ORDER
        )
        return [Event.model_validate_json(row[0]) for row in self._conn.execute(sql, params)]

    def event_count(self) -> int:
        return self._index.event_count()

    # -- entity scans ------------------------------------------------------

    def entity_summaries(
        self, *, prefix: str | None = None, limit: int | None = None
    ) -> list[EntitySummary]:
        """list_entities' answer as one indexed GROUP BY."""
        sql = (
            "SELECT entity_label, entity_detail, COUNT(DISTINCT attribute) FROM facts_idx"
        )
        params: list[Any] = []
        if prefix is not None:
            sql += " WHERE " + _prefix_sql("entity_label")
            params.extend(_prefix_params(prefix))
        # entity_detail IS NULL sorts first in SQLite, which is the same
        # order the Python version produced with `detail or ""`.
        sql += " GROUP BY entity_label, entity_detail ORDER BY entity_label, entity_detail"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [
            EntitySummary(entity_label=row[0], entity_detail=row[1], attribute_count=row[2])
            for row in self._conn.execute(sql, params)
        ]

    def entity_keys_for_label(self, label: str) -> list[str]:
        """Every stored entity key ("label" or "label#detail") a bare label
        resolves to -- what list_attributes needs to decide between
        answering and abstaining as AMBIGUOUS. entity_label is always
        populated (it falls back to `entity` for an undisambiguated fact),
        so one equality covers both shapes."""
        rows = self._conn.execute(
            "SELECT DISTINCT entity FROM facts_idx WHERE entity_label = ? ORDER BY entity",
            (label,),
        )
        return [row[0] for row in rows]

    def details_for_label(self, label: str) -> set[str]:
        """entity_detail values already on record for a bare label (§B9's
        entity_detail-required-on-collision rule)."""
        rows = self._conn.execute(
            "SELECT DISTINCT entity_detail FROM facts_idx WHERE entity_label = ? "
            "AND entity_detail IS NOT NULL",
            (label,),
        )
        return {row[0] for row in rows}

    # -- schema-on-write ---------------------------------------------------

    def entity_types_in_log(self) -> set[str]:
        """The "person" in "person:emma", for every fact on record -- the
        log-as-registry half of §B9's known-type check."""
        rows = self._conn.execute(
            "SELECT DISTINCT substr(entity, 1, instr(entity, ':') - 1) FROM facts_idx "
            "WHERE instr(entity, ':') > 1"
        )
        return {row[0] for row in rows}

    def attributes_for_entity_type(self, entity_type: str) -> set[str]:
        """Every attribute already used by a fact whose entity carries this
        type prefix."""
        rows = self._conn.execute(
            "SELECT DISTINCT attribute FROM facts_idx WHERE " + _prefix_sql("entity"),
            _prefix_params(f"{entity_type}:"),
        )
        return {row[0] for row in rows}

    # -- outcomes ----------------------------------------------------------

    def outcome_counts(self, needle: str) -> OutcomeCounts:
        """check_action's substring match over skuld's outcome events."""
        failures = self._conn.execute(
            "SELECT COUNT(*) FROM outcomes_idx WHERE type = 'outcome.abstention' "
            "AND instr(query_id, ?) > 0",
            (needle,),
        ).fetchone()[0]
        successes = self._conn.execute(
            "SELECT COUNT(*) FROM outcomes_idx WHERE type = 'outcome.verification' "
            "AND status = 'pass' AND instr(query_id, ?) > 0",
            (needle,),
        ).fetchone()[0]
        return OutcomeCounts(failures=int(failures), successes=int(successes))


def _prefix_upper_bound(prefix: str) -> str | None:
    """The smallest string strictly greater than every string starting with
    `prefix`, or None when there isn't a representable one."""
    if not prefix:
        return None
    nxt = ord(prefix[-1]) + 1
    if nxt > 0x10FFFF or 0xD800 <= nxt <= 0xDFFF:  # surrogates are not encodable
        return None
    return prefix[:-1] + chr(nxt)


def _prefix_sql(column: str) -> str:
    """A case-SENSITIVE prefix test, written so SQLite can use an index.

    Two halves, both needed. The `>= / <` range is what makes the test
    sargable -- an index on the column can seek straight to the prefix
    instead of scanning every row, which is the difference between 70 ms
    and single digits on a 100k-event workspace. The substr() equality is
    the exactness backstop: it is what actually defines the match, and it
    still applies when the range has no representable upper bound.

    Deliberately not LIKE 'x%': SQLite's LIKE is case-insensitive for ASCII
    by default, so list_entities(prefix="A") would have started returning
    "anna" -- a quiet widening of a filter, in the one tool that exists to
    tell a caller exactly what is on record. LIKE would also need its own
    wildcards escaped, since "%" and "_" are perfectly ordinary characters
    in an entity key ("user_id:7"). Neither problem arises here.
    """
    return f"({column} >= ? AND ({column} < ? OR ? IS NULL) AND substr({column}, 1, ?) = ?)"


def _prefix_params(prefix: str) -> list[Any]:
    upper = _prefix_upper_bound(prefix)
    return [prefix, upper, upper, len(prefix), prefix]


def events_match_log(query: IndexQuery, events: Sequence[Event]) -> bool:
    """True when the index returns exactly the log's events, in order --
    the G2 property this module relies on, asserted directly in tests."""
    return [e.id for e in query.events()] == [e.id for e in events]
