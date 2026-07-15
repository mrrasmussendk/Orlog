"""Assertion: the L3 contract, mirrors spec/schemas/assertion.json.

NOTE: this is the turn-1, generic epistemic-contract Assertion, kept only
for tests/test_assertion.py and tests/test_verification_gate.py, which
deliberately exercise the state-machine contract in the abstract. The REAL
Assertion the pipeline uses is `orlog.huginn.Assertion` -- a different,
incompatible shape (citations: list[str], allowed to be empty so heimdall's
V1 check can catch it). Do not import this one for pipeline code; import it
only via `from orlog.models.assertion import Assertion` in code that
specifically means the turn-1 abstract contract, never via `from orlog.models
import Assertion` (not re-exported at the package level, on purpose).

Design decision: `citations` uses pydantic's `Field(min_length=1)` on the list
itself. That single constraint is what makes "assertions without citations are
rejected" true at the type level — you cannot construct an Assertion with
citations=[] at all; pydantic raises ValidationError before any application
code runs. This is deliberately enforced here, in the model, rather than left
to huginn (derivation) or heimdall (verification) to check later: a rule this
central should not be possible to forget to call. (orlog.huginn.Assertion
takes the opposite approach, deliberately, for the reason explained there.)
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Citation(BaseModel):
    """A pointer to the evidence a claim rests on."""

    model_config = ConfigDict(extra="forbid")

    citation_id: str = Field(min_length=1)
    excerpt: str | None = None


class Assertion(BaseModel):
    """A claim plus the (non-empty) evidence it rests on."""

    model_config = ConfigDict(extra="forbid")

    assertion_id: str
    claim: str = Field(min_length=1)
    citations: list[Citation] = Field(min_length=1)
    derived_at: datetime
    derived_from: str | None = None
    retry_of: str | None = None
