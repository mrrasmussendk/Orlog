"""Pydantic models mirroring spec/schemas/*.json, one module per schema.

Each model file is named after the schema it mirrors (event.py <-> event.json,
and so on), so you can always find the code contract next to the spec
contract. Re-exported here so callers can do `from orlog.models import Event`
instead of reaching into each submodule.
"""

from orlog.models.assertion import Assertion, Citation
from orlog.models.event import Event
from orlog.models.outcome import Outcome
from orlog.models.projection import Projection
from orlog.models.retrieval import Candidate, Exclusion, RetrievalResult
from orlog.models.verification import VerificationChecks, VerificationResult

__all__ = [
    "Event",
    "Projection",
    "Candidate",
    "Exclusion",
    "RetrievalResult",
    "Citation",
    "Assertion",
    "VerificationChecks",
    "VerificationResult",
    "Outcome",
]
