"""heimdall: L4 verification -- the watchman at the gate (ORLOG-SPEC.md §4.5).

Design decision, and the most important one in this module: Heimdall is
constructed with its OWN ground-truth window table (`build_ground_truth`),
computed directly from the raw log -- NOT with a handle to verdandi's
projection. Spec §4.5 requires this explicitly: "the verifier is sovereign:
its truth source MUST be independent of L1's interpretation." build_ground_truth
duplicates a few lines of window-computation logic that also live in
verdandi.build_supersession_chains; that duplication is deliberate, not an
oversight -- if heimdall trusted verdandi's own windows, a bug or corruption
in the projection layer could poison the one gate meant to catch exactly
that (see spec §6 cell C3, not exercised this milestone, but the mechanism
here is what makes C3 passable later without changing heimdall at all).

The four checks (V1-V4) run in the order spec §4.5 gives, because it matters:
V1 is checked before any per-citation loop, since an empty citations list
would otherwise pass every per-citation check vacuously.

V4 (SUPPORTS) checks that the claim mentions BOTH the cited fact's own
"{entity}.{attribute}" key AND its value -- not just the value alone. A
value-only check has a real gap: if a projection mis-groups chains (cell C3),
retrieval can hand back a candidate that is really a DIFFERENT entity's fact
-- still a real, currently-valid citation, so V2 and V3 both pass. Checking
only "does the value appear in the claim" would then also pass, since the
claim was built from that same (wrong) fact's real value -- serving a
confidently wrong answer. Requiring the fact's own key to appear too catches
this, because a claim about "user:0.plan" naming a citation whose ground
truth says "user:1.plan" can never satisfy both halves.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from pydantic import BaseModel, ConfigDict, Field

from orlog.huginn import Assertion
from orlog.models.event import Event
from orlog.verdandi import OPEN_VALID_TO

UNCITED = "<uncited>"


class GroundTruthFact(BaseModel):
    """One entry in heimdall's own, independently-computed truth table."""

    model_config = ConfigDict(extra="forbid")

    fact_id: str
    entity: str
    attribute: str
    value: str
    valid_from: datetime
    valid_to: datetime
    # Transaction time: when this fact entered the log, as opposed to
    # valid_from/valid_to, which say when it was TRUE. Carried here so a
    # served answer can date its own evidence -- without it a citation shows
    # a validity window but not when the record learned it, and a correction
    # is indistinguishable from an original assertion.
    #
    # Optional for the same reason self_supported has a default: a truth
    # table built by hand (tests) or predating this field still constructs,
    # and None honestly means "unknown" rather than fabricating a timestamp.
    recorded_at: datetime | None = None
    # Runtime.remember()'s own write-time verdict: does this fact's
    # remembered text actually support the value it was recorded with?
    # Computed once, at write time, from the event's own payload -- never
    # recomputed here from a claim (that's V4/SUPPORTS' job, a different
    # check: claim-vs-citation, not text-vs-value). Defaults True for any
    # event that predates this field or was never given free text, so this
    # stays backward compatible rather than treating "unknown" as "poisoned".
    self_supported: bool = True
    # Runtime.remember()'s caller-supplied literal quote from the fact's own
    # `text` that grounds `value` (validated against text at write time --
    # see runtime.py). None for any event that predates this field or was
    # never given one, in which case a citation excerpt falls back to
    # `value` (see pipeline._citations_from).
    evidence_span: str | None = None

    @property
    def key(self) -> str:
        return f"{self.entity}.{self.attribute}"


def build_ground_truth(events: Sequence[Event]) -> dict[str, GroundTruthFact]:
    """Independently recompute (fact_id -> value + validity window) straight
    from the raw log. See module docstring for why this does not call
    verdandi.build_supersession_chains.
    """
    grouped: dict[tuple[str, str], list[Event]] = {}
    for event in events:
        if event.type != "fact":
            continue
        entity = event.payload.get("entity")
        attribute = event.payload.get("attribute")
        if entity is None or attribute is None:
            continue
        grouped.setdefault((entity, attribute), []).append(event)

    truth: dict[str, GroundTruthFact] = {}
    for (entity, attribute), group in grouped.items():
        ordered = sorted(group, key=lambda e: (e.occurred_at, e.recorded_at, e.id))
        for i, event in enumerate(ordered):
            valid_to = ordered[i + 1].occurred_at if i + 1 < len(ordered) else OPEN_VALID_TO
            truth[event.id] = GroundTruthFact(
                fact_id=event.id,
                entity=entity,
                attribute=attribute,
                value=str(event.payload.get("value")),
                valid_from=event.occurred_at,
                valid_to=valid_to,
                recorded_at=event.recorded_at,
                self_supported=event.payload.get("self_supported", True),
                evidence_span=event.payload.get("evidence_span"),
            )
    return truth


class VerificationFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    citation: str  # an event id, or the UNCITED sentinel
    code: str  # NOT_FOUND | EXPIRED | NOT_YET_VALID | UNSUPPORTED | UNCITED
    detail: str


class VerificationResult(BaseModel):
    """spec/ORLOG-SPEC.md §3.6."""

    model_config = ConfigDict(extra="forbid")

    assertion_query_id: str
    status: str  # "pass" | "fail"
    checked_at: datetime
    truth_version: str
    failures: list[VerificationFailure] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == "pass"


class Heimdall:
    """The reference verifier for the point-in-time-facts domain.

    Constructed once with a ground-truth table and a truth_version label;
    verify() is otherwise pure given (assertion, as_of, checked_at) -- same
    inputs, same result (spec §4.5 MUST: deterministic).
    """

    def __init__(self, truth: dict[str, GroundTruthFact], *, truth_version: str) -> None:
        self._truth = truth
        self.truth_version = truth_version

    def fact(self, event_id: str) -> GroundTruthFact | None:
        """Public accessor for pipeline.py to build citation excerpts/windows
        for a served Answer, without reaching into a private attribute.
        """
        return self._truth.get(event_id)

    def would_pass_validity(self, event_id: str, as_of: datetime) -> bool:
        """V2 + V3 only, against THIS verifier's own truth table. Used by
        pipeline.py to pre-filter candidates before the one permitted
        re-derivation (spec §5: "pre-filter by V2+V3").
        """
        fact = self._truth.get(event_id)
        return fact is not None and fact.valid_from <= as_of < fact.valid_to

    def verify(self, assertion: Assertion, as_of: datetime, *, checked_at: datetime) -> VerificationResult:
        # V1 UNCITED -- checked first; an empty citation list would otherwise
        # pass every per-citation check below vacuously.
        if not assertion.citations:
            return VerificationResult(
                assertion_query_id=assertion.query_id,
                status="fail",
                checked_at=checked_at,
                truth_version=self.truth_version,
                failures=[VerificationFailure(citation=UNCITED, code="UNCITED", detail="assertion has no citations")],
            )

        failures: list[VerificationFailure] = []
        for citation in assertion.citations:
            fact = self._truth.get(citation)

            # V2 EXISTS
            if fact is None:
                failures.append(
                    VerificationFailure(citation=citation, code="NOT_FOUND", detail="citation does not resolve against the sovereign truth table")
                )
                continue

            # V3 VALID
            if as_of < fact.valid_from:
                failures.append(
                    VerificationFailure(citation=citation, code="NOT_YET_VALID", detail=f"fact not valid until {fact.valid_from.isoformat()}")
                )
                continue
            if as_of >= fact.valid_to:
                failures.append(
                    VerificationFailure(citation=citation, code="EXPIRED", detail=f"fact superseded as of {fact.valid_to.isoformat()}")
                )
                continue

            # V4 SUPPORTS -- the claim must name both the fact's own key
            # (entity.attribute) and its value; see module docstring for why
            # value-alone is not enough.
            if fact.key not in assertion.claim or fact.value not in assertion.claim:
                failures.append(
                    VerificationFailure(
                        citation=citation,
                        code="UNSUPPORTED",
                        detail=f"claim does not support {fact.key} = {fact.value!r}",
                    )
                )

        return VerificationResult(
            assertion_query_id=assertion.query_id,
            status="fail" if failures else "pass",
            checked_at=checked_at,
            truth_version=self.truth_version,
            failures=failures,
        )
