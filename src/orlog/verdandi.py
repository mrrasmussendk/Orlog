"""verdandi: L1 projections -- what-is-becoming, rewoven from urd.

Design decisions, per ORLOG-SPEC.md §3.2/§3.3/§4.2:

1. `build_supersession_chains` is a pure function: same `events` + `builder`
   in, byte-identical (View, ProjectionVersion) out. It never calls
   datetime.now() -- `built_at` and `version` are supplied by the caller
   (spec §4.2's MUST: "build is pure and idempotent").

2. Scope: this milestone's only event shape is a `type="fact"` event whose
   payload looks like {"entity": "user:42", "attribute": "email",
   "value": "a@x.com"} -- an orlog-wide convention for this reference
   domain, layered on top of urd's general-purpose Event.type/payload
   fields (ORLOG-SPEC.md §A3); this module does not touch urd.py itself.

3. Validity windows are half-open [valid_from, valid_to) per spec §3.3: a
   fact's valid_from is its own occurred_at; valid_to is the next fact in
   the same chain's occurred_at, or the OPEN_VALID_TO sentinel
   (9999-12-31T00:00:00Z) if it's still current.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from pydantic import BaseModel, ConfigDict, Field

from orlog.models.event import Event

OPEN_VALID_TO = datetime(9999, 12, 31, tzinfo=timezone.utc)


class ProjectionVersion(BaseModel):
    """spec/ORLOG-SPEC.md §3.2."""

    model_config = ConfigDict(extra="forbid")

    projection: str
    version: int = Field(ge=1)
    built_from: str  # id of the last event (in the given order) included
    built_at: datetime
    builder: str
    quality: float | None = None


class ValidityWindow(BaseModel):
    """spec/ORLOG-SPEC.md §3.3. Half-open interval [valid_from, valid_to)."""

    model_config = ConfigDict(extra="forbid")

    fact_id: str
    valid_from: datetime
    valid_to: datetime


class SupersessionChainsView(BaseModel):
    """The View built by the 'supersession_chains' projection.

    `chains` groups type="fact" events by "{entity}::{attribute}", each list
    of event ids oldest-first. `windows` gives every one of those events'
    computed ValidityWindow, keyed by event id.
    """

    model_config = ConfigDict(extra="forbid")

    chains: dict[str, list[str]]
    windows: dict[str, ValidityWindow]


def build_supersession_chains(
    events: Sequence[Event],
    *,
    builder: str,
    built_at: datetime,
    version: int = 1,
) -> tuple[SupersessionChainsView, ProjectionVersion]:
    """Group type="fact" events into per-(entity, attribute) chains and
    compute each fact's validity window.

    `events` is expected in log (append) order -- as returned by
    EventLog.read_all() -- so `built_from` can record the last event this
    build incorporated. Raises ValueError on an empty input: a projection
    with zero source events isn't meaningful.
    """
    if not events:
        raise ValueError("cannot build supersession_chains from zero events")

    grouped: dict[str, list[Event]] = {}
    for event in events:
        if event.type != "fact":
            continue
        entity = event.payload.get("entity")
        attribute = event.payload.get("attribute")
        if entity is None or attribute is None:
            continue
        grouped.setdefault(f"{entity}::{attribute}", []).append(event)

    chains: dict[str, list[str]] = {}
    windows: dict[str, ValidityWindow] = {}
    for key, group in grouped.items():
        ordered = sorted(group, key=lambda e: (e.occurred_at, e.recorded_at, e.id))
        chains[key] = [e.id for e in ordered]
        for i, event in enumerate(ordered):
            valid_to = ordered[i + 1].occurred_at if i + 1 < len(ordered) else OPEN_VALID_TO
            windows[event.id] = ValidityWindow(
                fact_id=event.id, valid_from=event.occurred_at, valid_to=valid_to
            )

    view = SupersessionChainsView(chains=chains, windows=windows)
    projection_version = ProjectionVersion(
        projection="supersession_chains",
        version=version,
        built_from=events[-1].id,
        built_at=built_at,
        builder=builder,
    )
    return view, projection_version
