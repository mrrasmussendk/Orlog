"""verdandi_sessions: the 'session_summaries' projection (ORLOG-SPEC.md §B5).

Design decisions:

1. Session grouping key: `event.payload["session_id"]`, when present --
   events of type "utterance" / "decision" / "tool_result" (the
   conversational types spec §A3's type enum lists) are expected to carry
   one. Events without a session_id are skipped: they simply don't belong
   to any session digest, which is not an error.

2. "Deterministic template" (spec §B5 explicitly says v1.0 does NOT
   LLM-summarize sessions): the digest text is a fixed format -- event
   count, time range, then each event's payload.text on its own line --
   never a model-generated paragraph. This keeps the projection pure
   (spec §4.2 MUST: build() is a pure function of the events), exactly
   like verdandi.build_supersession_chains.

3. This is a separate module from verdandi.py rather than a second
   function in it, purely to keep each file under this project's ~200-line
   budget -- there is no other reason the two projections couldn't share a
   file.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from pydantic import BaseModel, ConfigDict

from orlog.models.event import Event
from orlog.verdandi import ProjectionVersion


class SessionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    event_count: int
    started_at: datetime
    ended_at: datetime
    digest: str
    event_ids: list[str]


class SessionSummariesView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sessions: dict[str, SessionSummary]


def _render_digest(session_id: str, events: list[Event]) -> str:
    header = f"Session {session_id}: {len(events)} events, {events[0].occurred_at.isoformat()} to {events[-1].occurred_at.isoformat()}"
    lines = [header]
    for event in events:
        text = event.payload.get("text")
        if text:
            lines.append(f"- [{event.actor}] {text}")
    return "\n".join(lines)


def build_session_summaries(
    events: Sequence[Event], *, builder: str, built_at: datetime, version: int = 1
) -> tuple[SessionSummariesView, ProjectionVersion]:
    """Group events by payload.session_id into a deterministic per-session
    digest. Pure: same events + builder in, byte-identical output out.
    """
    if not events:
        raise ValueError("cannot build session_summaries from zero events")

    grouped: dict[str, list[Event]] = {}
    for event in events:
        session_id = event.payload.get("session_id")
        if session_id is None:
            continue
        grouped.setdefault(session_id, []).append(event)

    if not grouped:
        raise ValueError("no events carried a payload.session_id -- nothing to summarize")

    sessions: dict[str, SessionSummary] = {}
    for session_id, group in grouped.items():
        ordered = sorted(group, key=lambda e: (e.occurred_at, e.recorded_at, e.id))
        sessions[session_id] = SessionSummary(
            session_id=session_id,
            event_count=len(ordered),
            started_at=ordered[0].occurred_at,
            ended_at=ordered[-1].occurred_at,
            digest=_render_digest(session_id, ordered),
            event_ids=[e.id for e in ordered],
        )

    view = SessionSummariesView(sessions=sessions)
    projection_version = ProjectionVersion(
        projection="session_summaries",
        version=version,
        built_from=events[-1].id,
        built_at=built_at,
        builder=builder,
    )
    return view, projection_version
