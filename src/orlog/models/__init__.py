"""Pydantic models mirroring spec/schemas/*.json, one module per schema.

These are the ORIGINAL, generic epistemic-contract models from this
project's first milestone (turn 1): abstract Event/Projection/Assertion/
VerificationResult/Outcome shapes proving the state-machine contracts in
isolation (see tests/test_assertion.py, tests/test_verification_gate.py,
which deliberately exercise these via direct submodule imports, e.g.
`from orlog.models.assertion import Assertion`).

Once the real, domain-specific v1.0 pipeline was built (ORLOG-SPEC.md Part
A/B), several of these names were superseded by incompatible types living
next to the code that actually uses them: `orlog.huginn.Assertion` (not
`orlog.models.assertion.Assertion` -- the old one still requires non-empty
citations, the opposite of the current design), `orlog.heimdall.
VerificationResult` (not `orlog.models.verification.VerificationResult` --
different shape entirely, aggregate failures vs. one-check-per-citation),
and `orlog.retrieval.Candidate`/`RetrievalResult` (not `orlog.models.
retrieval`'s versions -- different fields).

This package intentionally does NOT re-export those superseded names at the
top level anymore (it used to, and `from orlog.models import Assertion`
silently returned the wrong, incompatible contract -- a real landmine found
in review). Only `Event` is re-exported here, since `orlog.models.event.
Event` IS still the current, real Event type every other module uses.
Everything else -- including the turn-1 Assertion/Citation/VerificationChecks/
VerificationResult/Candidate/Exclusion/RetrievalResult/Projection/Outcome
models -- must be imported from its specific submodule
(`orlog.models.assertion`, `orlog.models.verification`, etc.), never from
this package directly, so it's never ambiguous which generation of a type
you're getting.
"""

from orlog.models.event import Event

__all__ = ["Event"]
