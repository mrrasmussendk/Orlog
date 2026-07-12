"""VerificationResult: the L4 contract, mirrors spec/schemas/verification.json.

Design decision: `passed` is not computed automatically from `checks` by a
validator. It is written by whatever plays heimdall (a real checker, or the
trivial in-memory fake used in tests), and this model then *validates* that
the two agree: passed must equal
`checks.exists and checks.currently_valid and checks.supports_claim`. Making
heimdall state its verdict explicitly (rather than deriving it silently)
mirrors DESIGN-PRINCIPLES.md principle 4 ("trust is a gate, not a property") —
the model's job is to catch a heimdall implementation that gets this wrong,
not to do heimdall's job for it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class VerificationChecks(BaseModel):
    """The three required scopes of a verification (spec/schemas/verification.json)."""

    model_config = ConfigDict(extra="forbid")

    exists: bool
    currently_valid: bool
    supports_claim: bool

    @property
    def all_pass(self) -> bool:
        return self.exists and self.currently_valid and self.supports_claim


class VerificationResult(BaseModel):
    """The outcome of verify(citation, claim, as_of)."""

    model_config = ConfigDict(extra="forbid")

    verification_id: str
    assertion_id: str
    citation_id: str
    claim: str = Field(min_length=1)
    as_of: datetime
    checks: VerificationChecks
    passed: bool
    reason: str | None = None

    @model_validator(mode="after")
    def _passed_matches_checks_and_has_reason_iff_failed(self) -> "VerificationResult":
        if self.passed != self.checks.all_pass:
            raise ValueError(
                "passed must equal checks.exists and checks.currently_valid "
                f"and checks.supports_claim (got passed={self.passed}, "
                f"checks={self.checks!r})"
            )
        if not self.passed and not self.reason:
            raise ValueError("reason is required when passed is False")
        if self.passed and self.reason:
            raise ValueError("reason must be null when passed is True")
        return self
