"""projections: the projection registry and its cache (ORLOG-SPEC.md §B12.2).

§B5 names the projections v1.0 ships. This module is the seam that lets a
*profile* ship its own: the `orlog.projections` entry-point group, a pure
`build(events, built_at) -> (view, ProjectionVersion)` protocol, and a
store that rebuilds every registered projection on `orlog replay` and
updates them incrementally on append.

Design decisions:

1. The protocol is `build(events, *, built_at)`, pure, exactly as spec
   §4.2 already requires of verdandi's own builder: same events in,
   byte-identical view out, no datetime.now(). `built_at` is handed in by
   the store; the `builder` string on the version row is the projection's
   own business, since it identifies who built the view, not who asked. A
   projection MAY also implement `update(view, version, new_events, *,
   built_at)` for a genuinely incremental rebuild; one that does not is
   simply rebuilt from its whole source set, so purity is the only
   requirement and incrementality is an optimization.

2. supersession_chains -- core's own, which every recall() depends on --
   is registered THROUGH this registry rather than beside it. A seam only
   one side uses is a seam nobody has tested; this way core's own hot path
   is the first consumer of the plugin contract.

3. `selects()` belongs to the projection, not the caller. verdandi's
   builder has always quietly ignored non-fact events; making that an
   explicit predicate means the store can filter once, cache the filtered
   set, and keep `built_from` (the last SELECTED event's id) identical to
   what Runtime computed before this module existed -- which matters,
   because built_from is folded into truth_version and therefore into the
   route-cache key (§B6).

4. The cache is keyed by (projection, known_as_of horizon, source head id,
   source length). The horizon is kept EXACT rather than rounded into a
   coarse time bucket: recorded_at is not monotonic (backfill is a
   first-class write, §A3), so two nearby horizons can select genuinely
   different event sets, and serving one's view for the other would be a
   stale answer -- the one failure this system exists to prevent. The head
   id + length pair is what makes a hit O(1) instead of O(n); together
   they also detect an append under a cache entry's feet.

5. `preselected` says whether the caller's list is ALREADY the projection's
   source set, and it is part of what a cache entry must match. Without it
   the (length, id-at-that-length) test is not safe: a view cached from the
   whole log and a later call passing only the fact events are two
   different source sets that can agree at one index by coincidence, and
   the entry would then be extended from the wrong base. Runtime passes
   preselected=True (its index query IS supersession_chains' selects());
   `orlog replay` passes the whole log and lets selects() do the filtering,
   which is O(n) but runs once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, Protocol, Sequence, runtime_checkable

from orlog.models.event import Event
from orlog.plugins import PROJECTIONS_GROUP, PluginError, load_group, resolve_mapping
from orlog.verdandi import ProjectionVersion, build_supersession_chains
from orlog.verdandi_sessions import build_session_summaries

DEFAULT_BUILDER = "orlog-runtime"
DEFAULT_CACHE_ENTRIES = 32


@runtime_checkable
class Projection(Protocol):
    """What a registered projection must provide (§B12.2)."""

    name: str
    version: int

    def build(self, events: Sequence[Event], *, built_at: datetime) -> tuple[Any, ProjectionVersion]:
        """Pure: same events in, byte-identical (view, version) out."""


@dataclass(frozen=True)
class ProjectionResult:
    """A built view, its version row, and the exact source events it was
    built from -- callers (Runtime.build_pipeline) need the last of those
    to build ground truth over the same set, not a re-derived one."""

    view: Any
    version: ProjectionVersion
    events: tuple[Event, ...]


class SupersessionChains:
    """Core's own projection (§B5), exposed through the registry seam."""

    name = "supersession_chains"
    version = 1

    def __init__(self, builder: str = DEFAULT_BUILDER) -> None:
        self.builder = builder

    @staticmethod
    def selects(event: Event) -> bool:
        return (
            event.type == "fact"
            and event.payload.get("entity") is not None
            and event.payload.get("attribute") is not None
        )

    def build(self, events: Sequence[Event], *, built_at: datetime) -> tuple[Any, ProjectionVersion]:
        return build_supersession_chains(
            events, builder=self.builder, built_at=built_at, version=self.version
        )


class SessionSummaries:
    """Core's second projection (§B5). It was specified and implemented, but
    nothing in the runtime ever built it -- there was no registry to put it
    in. Registering it here is what `orlog replay` now rebuilds alongside
    supersession_chains."""

    name = "session_summaries"
    version = 1

    def __init__(self, builder: str = DEFAULT_BUILDER) -> None:
        self.builder = builder

    @staticmethod
    def selects(event: Event) -> bool:
        return event.payload.get("session_id") is not None

    def build(self, events: Sequence[Event], *, built_at: datetime) -> tuple[Any, ProjectionVersion]:
        return build_session_summaries(
            events, builder=self.builder, built_at=built_at, version=self.version
        )


CORE_PROJECTIONS: tuple[type, ...] = (SupersessionChains, SessionSummaries)


def _check_projection(name: str, projection: Any, *, distribution: str) -> Projection:
    if isinstance(projection, type):
        try:
            projection = projection()
        except Exception as exc:  # noqa: BLE001
            raise PluginError(
                f"distribution {distribution!r} registered projection {name!r} as a class that "
                f"could not be instantiated with no arguments: {type(exc).__name__}: {exc}"
            ) from exc
    if not callable(getattr(projection, "build", None)):
        raise PluginError(
            f"distribution {distribution!r} registered projection {name!r} with {projection!r}, "
            "which has no build(events, *, built_at) method"
        )
    declared = getattr(projection, "name", None)
    if declared is not None and declared != name:
        raise PluginError(
            f"distribution {distribution!r} registered projection under the entry-point name "
            f"{name!r}, but the object calls itself {declared!r} -- the two must agree, because "
            "the name is what lands in index.sqlite's projections table"
        )
    return projection


class ProjectionRegistry:
    """Core's projections plus every profile's."""

    def __init__(self, projections: Iterable[tuple[str, Projection, str]] = ()) -> None:
        self._projections: dict[str, Projection] = {}
        self._distributions: dict[str, str] = {}
        for name, projection, distribution in projections:
            if name in self._projections:
                raise PluginError(
                    f"projection {name!r} is registered twice: by "
                    f"{self._distributions[name]!r} and by {distribution!r}"
                )
            self._projections[name] = projection
            self._distributions[name] = distribution

    @classmethod
    def load(
        cls, *, enabled: Iterable[str] | None = None, disabled: Iterable[str] | None = None
    ) -> "ProjectionRegistry":
        found: list[tuple[str, Projection, str]] = [
            (cls_.name, cls_(), "orlog") for cls_ in CORE_PROJECTIONS
        ]
        for loaded in load_group(PROJECTIONS_GROUP, enabled=enabled, disabled=disabled):
            for name, projection in resolve_mapping(loaded).items():
                if name in {p[0] for p in found if p[2] == "orlog"}:
                    raise PluginError(
                        f"distribution {loaded.distribution!r} tried to replace core projection "
                        f"{name!r}; core projections are not overridable"
                    )
                found.append(
                    (name, _check_projection(name, projection, distribution=loaded.distribution), loaded.distribution)
                )
        return cls(found)

    def __contains__(self, name: object) -> bool:
        return name in self._projections

    def __iter__(self):
        return iter(self._projections.items())

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._projections)

    def get(self, name: str) -> Projection:
        try:
            return self._projections[name]
        except KeyError:
            raise PluginError(
                f"no projection named {name!r} -- registered: {', '.join(self.names) or '(none)'}"
            ) from None

    def distribution_of(self, name: str) -> str:
        return self._distributions.get(name, "orlog")


@dataclass
class _Entry:
    result: ProjectionResult
    source_head_id: str
    source_len: int
    preselected: bool


def _horizon_key(known_as_of: datetime | None) -> str:
    return "*" if known_as_of is None else known_as_of.isoformat()


class ProjectionStore:
    """Builds, caches and persists registered projections.

    `index` is an EventIndex (or anything with `record_projection`); it is
    optional so an embedding caller can project without a workspace on disk.
    """

    def __init__(
        self,
        registry: ProjectionRegistry,
        *,
        index: Any | None = None,
        max_entries: int = DEFAULT_CACHE_ENTRIES,
    ) -> None:
        self.registry = registry
        self.index = index
        self.max_entries = max_entries
        self._entries: dict[tuple[str, str], _Entry] = {}
        self.hits = 0
        self.misses = 0
        self.incremental = 0

    # -- building ---------------------------------------------------------

    def view(
        self,
        name: str,
        events: Sequence[Event],
        *,
        built_at: datetime,
        known_as_of: datetime | None = None,
        preselected: bool = False,
    ) -> ProjectionResult | None:
        """The projection's view over `events`, from cache where possible.

        `preselected` promises that `events` is already exactly what this
        projection's selects() would keep -- see module docstring point 5.

        Returns None when the projection selects nothing from `events` --
        a projection over zero source events is not meaningful (verdandi
        says so by raising), and every caller already has to handle "no
        facts yet".
        """
        projection = self.registry.get(name)
        key = (name, _horizon_key(known_as_of))
        entry = self._entries.get(key)

        if entry is not None and entry.preselected == preselected and self._extends(entry, events):
            if entry.source_len == len(events):
                self.hits += 1
                return entry.result
            tail = events[entry.source_len :]
            result = self._extend(
                projection, entry, tail, built_at=built_at, preselected=preselected
            )
            if result is not None:
                self.incremental += 1
                self._store(key, result, events, preselected)
                return result

        self.misses += 1
        result = self._build(projection, events, built_at=built_at, preselected=preselected)
        if result is None:
            self._entries.pop(key, None)
            return None
        self._store(key, result, events, preselected)
        return result

    @staticmethod
    def _extends(entry: _Entry, events: Sequence[Event]) -> bool:
        """Is `events` the cached source set plus zero or more appends?
        O(1): the log is append-only, so a matching (length, id-at-that-
        length) pair can only mean the same prefix."""
        if len(events) < entry.source_len or entry.source_len == 0:
            return False
        return events[entry.source_len - 1].id == entry.source_head_id

    def _select(
        self, projection: Projection, events: Sequence[Event], *, preselected: bool = False
    ) -> list[Event]:
        if preselected:
            return list(events)
        selects: Callable[[Event], bool] | None = getattr(projection, "selects", None)
        return list(events) if selects is None else [e for e in events if selects(e)]

    def _selects(self, projection: Projection, event: Event) -> bool:
        selects: Callable[[Event], bool] | None = getattr(projection, "selects", None)
        return True if selects is None else bool(selects(event))

    def _build(
        self,
        projection: Projection,
        events: Sequence[Event],
        *,
        built_at: datetime,
        preselected: bool = False,
    ) -> ProjectionResult | None:
        selected = self._select(projection, events, preselected=preselected)
        if not selected:
            return None
        view, version = projection.build(selected, built_at=built_at)
        return self._record(ProjectionResult(view=view, version=version, events=tuple(selected)))

    def _extend(
        self,
        projection: Projection,
        entry: _Entry,
        tail: Sequence[Event],
        *,
        built_at: datetime,
        preselected: bool = False,
    ) -> ProjectionResult | None:
        """Fold newly appended events into a cached view.

        Uses the projection's own `update()` when it has one; otherwise
        rebuilds from the widened source set. Either way the result is the
        same view a cold build would produce -- `update()` is an
        optimization a projection opts into, never a different answer.
        """
        new_selected = self._select(projection, tail, preselected=preselected)
        if not new_selected:
            # Nothing this projection cares about was appended: the cached
            # view is still exactly right, only the source length moved.
            return entry.result
        widened = list(entry.result.events) + new_selected
        update = getattr(projection, "update", None)
        if callable(update):
            view, version = update(
                entry.result.view, entry.result.version, new_selected, built_at=built_at
            )
            return self._record(ProjectionResult(view=view, version=version, events=tuple(widened)))
        view, version = projection.build(widened, built_at=built_at)
        return self._record(ProjectionResult(view=view, version=version, events=tuple(widened)))

    def _record(self, result: ProjectionResult) -> ProjectionResult:
        if self.index is not None and hasattr(self.index, "record_projection"):
            self.index.record_projection(result.version)
        return result

    def _store(
        self,
        key: tuple[str, str],
        result: ProjectionResult,
        events: Sequence[Event],
        preselected: bool,
    ) -> None:
        if len(self._entries) >= self.max_entries and key not in self._entries:
            self._entries.pop(next(iter(self._entries)))
        self._entries[key] = _Entry(
            result=result,
            source_head_id=events[-1].id if events else "",
            source_len=len(events),
            preselected=preselected,
        )

    # -- lifecycle --------------------------------------------------------

    def invalidate(self, name: str | None = None) -> None:
        """Drop cached views (all, or one projection's). Used by `orlog
        replay`, which must prove a view is rebuildable from the log rather
        than served from whatever this process happened to retain."""
        for key in [k for k in self._entries if name is None or k[0] == name]:
            self._entries.pop(key)

    def rebuild_all(
        self, events: Sequence[Event], *, built_at: datetime
    ) -> dict[str, ProjectionVersion]:
        """Rebuild every registered projection from scratch (`orlog
        replay`). Returns the version row each one produced; a projection
        that selects nothing from this log is omitted."""
        self.invalidate()
        built: dict[str, ProjectionVersion] = {}
        for name, _ in self.registry:
            result = self.view(name, events, built_at=built_at)
            if result is not None:
                built[name] = result.version
        return built

    def on_append(self, event: Event, *, built_at: datetime) -> None:
        """Fold one newly appended event into every live cached view --
        §B12.2's "update incrementally on append".

        Only the unbounded (known_as_of-free) views are extended. A view
        pinned to a past transaction-time horizon must not absorb an event
        recorded after it; and because recorded_at is not monotonic, an
        event recorded BEFORE the horizon belongs somewhere in the middle
        of that view's source set, not on the end -- so those entries are
        dropped and rebuilt rather than patched.

        A preselected entry's source list is the projection's own events,
        so an event this projection does not select never appears in it and
        the entry is left completely alone. A non-preselected entry's
        source list is everything, so its length grows for every append
        whether the projection cares about the event or not.
        """
        for key in list(self._entries):
            name, horizon = key
            if horizon != "*":
                self._entries.pop(key)
                continue
            entry = self._entries[key]
            projection = self.registry.get(name)
            if entry.preselected and not self._selects(projection, event):
                continue
            result = self._extend(
                projection, entry, [event], built_at=built_at, preselected=entry.preselected
            )
            if result is None:
                self._entries.pop(key)
                continue
            self.incremental += 1
            self._entries[key] = _Entry(
                result=result,
                source_head_id=event.id,
                source_len=entry.source_len + 1,
                preselected=entry.preselected,
            )

    def stats(self) -> dict[str, int]:
        return {
            "projection_cache_hits": self.hits,
            "projection_cache_misses": self.misses,
            "projection_incremental_updates": self.incremental,
            "projection_cache_entries": len(self._entries),
        }
