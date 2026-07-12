"""Outcome: the L5 contract, mirrors spec/schemas/outcome.json.

Design decision: `final_state` reuses the same EpistemicState enum that drives
states.py, instead of redefining its own string enum, so there is exactly one
place in the codebase that spells "verified" / "abstained" / "reinforced".
It is restricted to those three values with a validator, because an Outcome
by definition only exists once a claim has reached a *terminal* state — an
Outcome with final_state=OBSERVED would not make sense (states.py has no
import of orlog.models, so importing it here does not create a cycle).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orlog.states import EpistemicState

_TERMINAL_STATES = {
    EpistemicState.VERIFIED,
    EpistemicState.ABSTAINED,
    EpistemicState.REINFORCED,
}


class Outcome(BaseModel):
    """The terminal record of what happened to an assertion, fed back to skuld."""

    model_config = ConfigDict(extra="forbid")

    outcome_id: str
    assertion_id: str
    verification_ids: list[str] = Field(min_length=1)
    final_state: EpistemicState
    recorded_at: datetime
    appended_event_id: str | None = None

    @field_validator("final_state")
    @classmethod
    def _must_be_terminal(cls, value: EpistemicState) -> EpistemicState:
        if value not in _TERMINAL_STATES:
            raise ValueError(
                f"final_state must be one of {sorted(s.value for s in _TERMINAL_STATES)}, "
                f"got {value.value!r}"
            )
        return value
