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

V4 (SUPPORTS) requires the claim to BE the cited fact, in the canonical
"{entity}.{attribute} = {value}" shape both derivers are contracted to emit
(huginn.ScriptedDeriver builds it directly; huginn_llm's SYSTEM_PROMPT
mandates it). It is an exact match on each half after whitespace/case
normalization -- deliberately NOT substring containment.

Containment was the original implementation and it was unsound in two
separate ways, both of which let a claim through the gate that the cited
fact did not support:

1. A value is a substring of longer, different values. Ground truth
   "user:1.plan = Pro" would verify the claim "user:1.plan = Professional
   Enterprise (unlimited seats), renewing 2027-01-01". Numerically it is
   worse: value "5" is contained in "500", value "0" in "1000", and a bare
   digit from the entity name ("user:1") satisfies a claim of "9999". The
   key half was equally porous -- key "user:1.plan" is a substring of a
   claim about "user:1.plan_renewal".
2. Containment constrains only what the claim CONTAINS, never what else it
   says. "user:1.plan = pro" is contained in "user:1.plan is not pro" and in
   "user:1.plan = pro, and user:1.card = 4111-1111-1111-1111" -- so a
   negation, or an arbitrary hallucinated rider, shipped under VERIFIED.

Requiring equality closes both: anything the deriver adds, negates, or
substitutes changes one of the two normalized halves and fails the check.
The C3 mis-grouping defense the key half was added for still holds a
fortiori -- a claim about "user:0.plan" cannot equal a ground truth keyed
"user:1.plan".
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from pydantic import BaseModel, ConfigDict, Field

from orlog.huginn import Assertion
from orlog.models.event import Event
from orlog.verdandi import OPEN_VALID_TO

UNCITED = "<uncited>"

#: Separator between the key and value halves of a canonical claim. Both
#: derivers emit exactly this (huginn.py's f"{query} = {best.content}" and
#: huginn_llm.py's SYSTEM_PROMPT), so V4 can split on it rather than guess.
CLAIM_SEP = "="


def _normalize(text: str) -> str:
    """Fold the incidental variation a deriver can introduce -- surrounding
    and repeated whitespace, letter case -- without folding anything that
    changes meaning. Used on both halves of a claim before comparing them
    to ground truth, so "user:1.plan  =  PRO" still verifies against
    "pro" while "not pro" and "professional" do not.
    """
    return " ".join(text.split()).casefold()


def claim_supports(claim: str, key: str, value: str) -> bool:
    """True when `claim` is exactly the canonical assertion of `key = value`.

    Split on the FIRST separator only: a value is free to contain "="
    (a base64 token, a query string) and must survive the round trip, whereas
    a key -- "{entity}.{attribute}" -- never contains one.
    """
    claim_key, sep, claim_value = claim.partition(CLAIM_SEP)
    if not sep:
        return False
    return _normalize(claim_key) == _normalize(key) and _normalize(claim_value) == _normalize(value)


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

            # V4 SUPPORTS -- the claim must BE this fact, in the canonical
            # "key = value" shape, not merely contain its key and value; see
            # the module docstring for the two unsoundnesses containment had.
            if not claim_supports(assertion.claim, fact.key, fact.value):
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
