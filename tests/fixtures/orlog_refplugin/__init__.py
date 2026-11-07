"""orlog_refplugin: the no-op reference profile (ORLOG-SPEC.md §B12).

Spec §B12's acceptance criterion is that a reference plugin registering one
event type, one projection and one tool loads through entry points and
passes. This is that plugin. It is deliberately trivial -- it proves the
SEAMS, not any particular memory design -- and it lives under tests/ rather
than in src/ because orlog core declares no entry points of its own.

tests/test_plugins.py installs it into a temporary directory as a real
distribution (a .dist-info with an entry_points.txt) and puts that on
sys.path, so discovery goes through importlib.metadata exactly as it would
for a pip-installed profile. Nothing is monkeypatched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field

from orlog.models.event import Event
from orlog.verdandi import ProjectionVersion

TYPE_NAME = "x.refplugin.note"


class NotePayload(BaseModel):
    """The payload model for x.refplugin.note."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1)
    topic: str = "general"


EVENT_TYPES = {TYPE_NAME: NotePayload}


class NotesByTopicView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topics: dict[str, list[str]]


class NotesByTopic:
    """topic -> the ids of the notes filed under it."""

    name = "notes_by_topic"
    version = 1

    @staticmethod
    def selects(event: Event) -> bool:
        return event.type == TYPE_NAME

    def build(
        self, events: Sequence[Event], *, built_at: datetime
    ) -> tuple[NotesByTopicView, ProjectionVersion]:
        topics: dict[str, list[str]] = {}
        for event in events:
            topics.setdefault(event.payload.get("topic", "general"), []).append(event.id)
        return (
            NotesByTopicView(topics=topics),
            ProjectionVersion(
                projection=self.name,
                version=self.version,
                built_from=events[-1].id,
                built_at=built_at,
                builder="orlog-refplugin",
            ),
        )


PROJECTIONS = {NotesByTopic.name: NotesByTopic}


def notes_about(runtime: Any, topic: str, limit: int = 20) -> dict:
    """Every note filed under `topic`, newest last. Reference profile tool."""
    events = runtime.query.events(types=[TYPE_NAME], limit=None)
    notes = [
        {"event_id": e.id, "body": e.payload["body"]}
        for e in events
        if e.payload.get("topic", "general") == topic
    ]
    return {"topic": topic, "notes": notes[-limit:]}


TOOLS = {"notes_about": notes_about}
