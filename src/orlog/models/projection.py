"""Projection: the L1 contract, mirrors spec/schemas/projection.json.

Projections are *not* frozen like Event: a new projection with the same
projection_id is never mutated either (you'd build a new Projection object
with a bumped version instead), but nothing in this milestone writes
projections yet, so there is no code path that could mutate one in place.
Frozen is left off here deliberately to keep this model simple until
verdandi.py (the projection layer) actually exists and we know what it needs.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Projection(BaseModel):
    """A pure, versioned view computed from a slice of the log."""

    model_config = ConfigDict(extra="forbid")

    projection_id: str
    view_name: str = Field(min_length=1)
    version: int = Field(ge=1)
    computed_at: datetime
    source_event_ids: list[str] = Field(min_length=1)
    quality_score: float | None = Field(default=None, ge=0, le=1)
    data: dict
