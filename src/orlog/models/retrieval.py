"""RetrievalResult: the L2 contract, mirrors spec/schemas/retrieval.json.

Design decision: `excluded` has no default and is a required field (not
`= []`), even though an empty list is a perfectly normal value. This forces
whoever builds a RetrievalResult to explicitly decide "nothing was excluded"
rather than silently forgetting the field — the no-silent-caps rule from
DESIGN-PRINCIPLES.md principle 12 is about honesty, and an accidentally-omitted
field defaulting to "nothing excluded" would be exactly the silent gap it
warns against.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Candidate(BaseModel):
    """One retrieved candidate, grounded in a specific log event."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    score: float
    excerpt: str | None = None


class Exclusion(BaseModel):
    """A reason some candidates were left out, and how many."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1)
    count: int = Field(ge=1)


class RetrievalResult(BaseModel):
    """The result of retrieve(query, as_of, view, k)."""

    model_config = ConfigDict(extra="forbid")

    retrieval_id: str
    query: str = Field(min_length=1)
    as_of: datetime
    view: str = Field(min_length=1)
    k: int = Field(ge=0)
    results: list[Candidate]
    excluded: list[Exclusion]
