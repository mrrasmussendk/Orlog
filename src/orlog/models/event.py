"""Event: the L0 contract, mirrors ORLOG-SPEC.md §A3 (v1.0).

Design decisions:

1. Split into EventDraft (what a caller constructs) and Event (what urd.py
   returns after appending). Spec is explicit that `id`, `recorded_at`, and
   `prev_hash` are "assigned by the log, never the caller" -- a caller
   physically cannot supply them if the type they build doesn't have those
   fields. EventDraft is that type; Event is the full, immutable record.

2. Event is frozen (model_config frozen=True), same reasoning as before:
   once built, no Python code can mutate a field in place, on top of urd.py
   only ever appending.

3. `type` uses spec's v1.0 vocabulary (payload-carrying types like "fact",
   plus the outcome.* types skuld.py writes) rather than the free-form
   "fact_stated" convention used before this milestone -- see
   ORLOG-SPEC.md §A3's type enum.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0"


class EventDraft(BaseModel):
    """What a caller builds before calling EventLog.append()."""

    model_config = ConfigDict(extra="forbid")

    occurred_at: datetime
    actor: str = Field(min_length=1)
    type: str = Field(min_length=1)
    payload: dict
    entities: list[str] = Field(default_factory=list)
    supersedes_hint: str | None = None


class Event(BaseModel):
    """One atomic, append-only, hash-chained fact in the log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    occurred_at: datetime
    recorded_at: datetime
    actor: str = Field(min_length=1)
    type: str = Field(min_length=1)
    payload: dict
    entities: list[str] = Field(default_factory=list)
    supersedes_hint: str | None = None
    schema_version: str = SCHEMA_VERSION
    prev_hash: str

    @model_validator(mode="after")
    def _recorded_after_occurred(self) -> "Event":
        # A bi-temporal sanity check (DESIGN-PRINCIPLES.md principle 7): you
        # cannot record something before it happened. Equal timestamps are
        # fine (e.g. synchronous logging).
        if self.recorded_at < self.occurred_at:
            raise ValueError(
                "recorded_at cannot be earlier than occurred_at: "
                f"occurred_at={self.occurred_at!r}, recorded_at={self.recorded_at!r}"
            )
        return self
