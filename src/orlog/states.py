"""The epistemic state machine from DESIGN-PRINCIPLES.md.

    OBSERVED -> INTERPRETED -> RETRIEVED -> ASSERTED -> VERIFIED -> REINFORCED
                                                  |
                                                  +-> ABSTAINED

Design decision: ASSERTED can go straight to ABSTAINED. This is deliberate,
not a shortcut. The design doc says a failed verification triggers exactly
one re-derivation, then abstention. A "re-derivation" is huginn producing a
*brand new* Assertion object (see Assertion.retry_of in
src/orlog/models/assertion.py) — it is not a new state for the *same*
assertion to sit in. So the state machine only ever needs to know two things
about any single assertion: did it reach VERIFIED, or did it (after however
many derivation attempts orlog's orchestration allowed) end in ABSTAINED?
The "exactly one retry" rule is a policy enforced by whatever code drives
this state machine (see tests/test_verification_gate.py for the reference
flow), not a fact this graph needs to represent on its own — the graph only
has to make ASSERTED -> ABSTAINED a legal move so that policy is *allowed* to
happen, and make everything else illegal so it can't happen by accident.

This module intentionally has zero imports from the rest of orlog, so it can
be imported from anywhere (including orlog/models/outcome.py) without any
risk of a circular import.
"""

from __future__ import annotations

from enum import Enum


class EpistemicState(str, Enum):
    """Where a single piece of knowledge is in its lifecycle."""

    OBSERVED = "observed"
    INTERPRETED = "interpreted"
    RETRIEVED = "retrieved"
    ASSERTED = "asserted"
    VERIFIED = "verified"
    ABSTAINED = "abstained"
    REINFORCED = "reinforced"


# The legal moves. Read as "from this state, you may move to any of these."
# TERMINAL_STATES (ABSTAINED, REINFORCED) map to an empty set: nothing may
# follow them.
ALLOWED_TRANSITIONS: dict[EpistemicState, frozenset[EpistemicState]] = {
    EpistemicState.OBSERVED: frozenset({EpistemicState.INTERPRETED}),
    EpistemicState.INTERPRETED: frozenset({EpistemicState.RETRIEVED}),
    EpistemicState.RETRIEVED: frozenset({EpistemicState.ASSERTED}),
    EpistemicState.ASSERTED: frozenset({EpistemicState.VERIFIED, EpistemicState.ABSTAINED}),
    EpistemicState.VERIFIED: frozenset({EpistemicState.REINFORCED}),
    EpistemicState.ABSTAINED: frozenset(),
    EpistemicState.REINFORCED: frozenset(),
}

TERMINAL_STATES: frozenset[EpistemicState] = frozenset(
    {EpistemicState.ABSTAINED, EpistemicState.REINFORCED}
)


class IllegalTransitionError(ValueError):
    """Raised when code tries to move a claim to a state it cannot legally reach."""

    def __init__(self, current: EpistemicState, target: EpistemicState) -> None:
        self.current = current
        self.target = target
        allowed = sorted(s.value for s in ALLOWED_TRANSITIONS[current])
        super().__init__(
            f"illegal transition: {current.value} -> {target.value} "
            f"(from {current.value}, only {allowed or '[terminal, no moves]'} are allowed)"
        )


def transition(current: EpistemicState, target: EpistemicState) -> EpistemicState:
    """Validate a move from `current` to `target`, returning `target` if legal.

    Raises IllegalTransitionError otherwise. This function does not hold any
    state itself — callers own the "current state" of whatever they are
    tracking (an Assertion, a test, ...) and call this each time they want to
    advance it, e.g.:

        state = EpistemicState.OBSERVED
        state = transition(state, EpistemicState.INTERPRETED)
    """

    if target not in ALLOWED_TRANSITIONS[current]:
        raise IllegalTransitionError(current, target)
    return target
