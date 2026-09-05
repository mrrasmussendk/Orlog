"""huginn: L3 derivation -- thought, sent out for answers (ORLOG-SPEC.md §3.5/§4.4).

Design decisions:

1. `Assertion.citations` is a plain `list[str]` and is allowed to be empty at
   the type level -- unlike this project's earlier (turn-1) Assertion model,
   which enforced non-empty citations at construction. Spec §4.4 says a
   Deriver "MUST reject its own output and raise UncitedAssertion if the
   model returns no citations" (checked here, at L3) AND heimdall's V1 check
   independently rejects an empty-citation Assertion at L4 ("T1 is enforced
   here AND at L4 -- defense in depth", spec §4.4). Defense in depth only
   works if both layers can actually encounter the empty-citations case, so
   the type itself must allow constructing one.

2. ScriptedDeriver is a deterministic stand-in for a real LLM deriver: it
   picks the highest-scored candidate and returns its content as the claim,
   citing that one candidate. No model call, no randomness -- every test
   using it is reproducible without an API key. The real LLM-backed Deriver
   is a follow-up step; only the implementation changes. Swapping it in
   later shouldn't require changing anything downstream, since heimdall only
   ever looks at the Assertion produced, never at how it was produced.

3. `tokens`/`latency_ms` (spec §A3 observability additions) are optional and
   left None by ScriptedDeriver -- it makes no model call, so there is
   nothing honest to report. huginn_llm.LLMDeriver populates both.

4. DeriverFailure and its three subclasses are the taxonomy spec §B4/§A4 P3
   requires: "Deriver-level failures (timeout, malformed output after
   repair, empty citations after retry) -> abstain DERIVATION_FAILED --
   never a protocol violation, never an unverified answer." pipeline.py
   catches DeriverFailure around every derive() call and turns it straight
   into an abstention using `.reason`; it is never allowed to propagate as
   an unhandled exception.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from orlog.retrieval import Candidate


class Assertion(BaseModel):
    """spec/ORLOG-SPEC.md §3.5."""

    model_config = ConfigDict(extra="forbid")

    query_id: str
    claim: str = Field(min_length=1)
    citations: list[str] = Field(default_factory=list)
    derived_by: str
    derived_at: datetime
    route: str  # "fresh" | "cache" | "rederived"
    tokens: dict[str, int] | None = None  # {"in": int, "out": int}
    latency_ms: float | None = None


class DeriverFailure(Exception):
    """Base class for derivation-level failures (spec §A4 P3's abstention
    taxonomy). `reason` is the abstention reason code pipeline.py surfaces
    on the Answer.
    """

    reason: str = "DERIVATION_FAILED"


class UncitedAssertion(DeriverFailure):
    """Raised by a Deriver that would otherwise produce zero citations --
    empty candidates (ScriptedDeriver's defensive case), or an LLM deriver
    that still has no valid citations after its one repair retry.
    """


class DeriverInsufficientEvidence(DeriverFailure):
    """The model explicitly said the provided facts don't answer the query
    (the literal INSUFFICIENT token, spec §B4's prompt contract)."""

    reason = "DERIVER_INSUFFICIENT"


class DeriverTimeout(DeriverFailure):
    """The model call did not complete within the configured timeout."""

    reason = "DERIVER_TIMEOUT"


class DeriverTruncated(DeriverFailure):
    """The model stopped because it hit the max_tokens cap, so whatever it
    produced is an incomplete JSON object rather than an answer.

    Distinguished from UncitedAssertion because the remedy is different and
    the repair retry cannot help: retrying at the same cap truncates in the
    same place, so this abstains immediately with an accurate reason instead
    of paying for a second identical call.
    """

    reason = "DERIVER_TRUNCATED"


class DeriverProviderError(DeriverFailure):
    """The model provider failed for any reason other than a timeout -- rate
    limited, overloaded, unauthorized, unreachable, or answering with a
    response object the adapter could not read.

    These all arrive as provider-SDK exception types, which pipeline.py must
    never see: it abstains on DeriverFailure, so anything outside the
    taxonomy escapes answer() and crashes the caller instead of producing
    the abstention spec §A4 P3 requires.
    """

    reason = "DERIVATION_FAILED"


class Deriver(Protocol):
    def derive(
        self,
        query: str,
        as_of: datetime,
        candidates: list[Candidate],
        *,
        query_id: str,
        derived_at: datetime,
        route: str,
    ) -> Assertion: ...


class ScriptedDeriver:
    """A deterministic, non-LLM Deriver: cites the single highest-scored
    candidate, verbatim. Ties are broken by candidate order (stable sort).
    """

    NAME = "scripted-deriver-v1"

    def derive(
        self,
        query: str,
        as_of: datetime,
        candidates: list[Candidate],
        *,
        query_id: str,
        derived_at: datetime,
        route: str,
    ) -> Assertion:
        if not candidates:
            raise UncitedAssertion(f"no candidates to derive an assertion from for query_id={query_id!r}")

        best = max(candidates, key=lambda c: c.score)
        return Assertion(
            query_id=query_id,
            claim=f"{query} = {best.content}",
            citations=[best.event_id],
            derived_by=self.NAME,
            derived_at=derived_at,
            route=route,
        )
