"""orlog: a spec-with-a-runtime for trustworthy AI agent memory.

See docs/ORLOG-SPEC.md for the normative protocol this package implements
(docs/DESIGN-PRINCIPLES.md is the design rationale behind it). This package
ships the contracts (pydantic models mirroring spec/schemas/), the
append-only event log (urd.py), and the epistemic state machine (states.py).

The L1-L5 reference pipeline (verdandi, retrieval, huginn, heimdall, muninn,
skuld, pipeline) is intentionally NOT re-exported here: each of those modules
defines its own spec-shaped types (e.g. orlog.huginn.Assertion,
orlog.heimdall.VerificationResult) that are distinct from the models in
orlog.models -- collapsing them into one flat namespace would silently shadow
one Assertion with another. Import from the specific module you need, e.g.
`from orlog.pipeline import Pipeline`.
"""

from orlog.states import EpistemicState, IllegalTransitionError, transition
from orlog.urd import EventLog

__all__ = [
    "EpistemicState",
    "IllegalTransitionError",
    "transition",
    "EventLog",
]
