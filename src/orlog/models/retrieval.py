"""retrieval: the L0 record of a READ, as opposed to a write.

Design decisions:

1. urd's events have always described what was *written* (type="fact") or
   what a write's verification concluded (skuld's outcome.* types). None of
   them describe what a reader was actually told. Two agents querying the
   same log can end up with different contexts -- different horizons,
   different valid-time questions, different abstentions -- so the retrieval
   itself is data, and reconstructing an agent's context after the fact is
   impossible without it.

2. This is a payload shape, not a new Event class. It rides on the existing
   hash-chained log as type="retrieval", so it inherits the same integrity
   guarantee as every fact, and every fact-shaped projection already skips
   it by type exactly as it skips outcome.*.

3. `known_as_of` is stored RESOLVED, never as the caller passed it. If the
   caller omitted it and orlog defaulted to "now", the default is what gets
   written down -- with `horizon_defaulted` marking that it was a default
   rather than a choice. A horizon the system picked and did not record is
   the same axis-collapse bug that made recall() non-reconstructible in the
   first place, one level up.

4. Citations are stored whole, not as bare ids. A context rebuilt from this
   trace needs the excerpt that went into the prompt; a bare id would force
   the reader back into the log and reintroduce exactly the ambiguity
   (which revision? at which horizon?) the trace exists to remove.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RetrievalQuery(BaseModel):
    """What was asked, on both time axes."""

    model_config = ConfigDict(extra="forbid")

    entity: str | None
    attribute: str | None
    query: str  # the raw query string as the caller sent it
    valid_at: datetime
    known_as_of: datetime
    horizon_defaulted: bool = False


class RetrievalCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    excerpt: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    recorded_at: datetime | None = None


class RetrievalResult(BaseModel):
    """What the reader was told."""

    model_config = ConfigDict(extra="forbid")

    verified: bool
    value: str | None
    claim: str | None
    abstained: bool
    reasons: list[str] = Field(default_factory=list)
    citations: list[RetrievalCitation] = Field(default_factory=list)
    route: str | None = None
    truth_version: str | None = None


class RetrievalRecord(BaseModel):
    """One step of one agent run: the payload of a type="retrieval" event."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    step: int = Field(ge=0)
    query: RetrievalQuery
    result: RetrievalResult
    latency_ms: float = Field(ge=0)
