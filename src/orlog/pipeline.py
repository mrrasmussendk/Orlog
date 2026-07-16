"""pipeline: the answer() algorithm -- ORLOG-SPEC.md §5/§A3/§A4, normative.

Wires every layer built this milestone into one call: muninn (cache) ->
heimdall (verify) -> retrieval (L2) -> huginn (derive) -> heimdall (verify)
-> exactly one re-derivation -> abstain. Every VERIFIED/ABSTAINED outcome is
also recorded to skuld (T5), and every pass/fail transition is validated
against states.py's legal-transition graph (T6: illegal transitions MUST
raise) -- tying this pipeline back to the epistemic state machine built in
the first milestone, rather than reimplementing its rules here.

Design decisions:

1. `answer()` takes `now` explicitly (used for checked_at, derived_at, and
   cache bookkeeping) rather than calling datetime.now() internally, so
   every conformance test stays deterministic and reproducible without a
   real clock -- the same reason ScriptedDeriver exists instead of a real
   model call.

2. `Answer` is ONE type for both a served answer and an abstention, per
   spec §A3: "Abstention: 'abstained': true, 'claim': null, 'reasons': [...]"
   describes overriding fields on the SAME object shape, not a different
   type. `answer()` therefore always returns an Answer; callers branch on
   `.verified`/`.abstained`, not on isinstance(). Internally, the L3
   Assertion (bare-ULID citations, no excerpt/window) is still a distinct,
   separate object -- Answer.citations are enriched (excerpt + validity
   window) by looking each one up in heimdall's own ground truth via the
   new `Heimdall.fact()` accessor, since that's the sovereign source, not
   whatever retrieval happened to hand back.

3. `query_id` is a parameter of `answer()` (needed for ledger bookkeeping
   and Assertion.query_id) but is NOT a field on the returned Answer --
   spec §A3's Answer schema has no query_id field.

4. Every `deriver.derive()` call is wrapped in `try/except DeriverFailure`.
   spec §A4 P3: "Deriver-level failures (timeout, malformed output after
   repair, empty citations after retry) -> abstain DERIVATION_FAILED --
   never a protocol violation, never an unverified answer." A deriver
   failure abstains immediately -- it does NOT consume the one permitted
   re-derivation (that retry is specifically for citations that failed
   heimdall's verification, a different failure mode entirely).

5. The cache key includes `self.verifier.truth_version` (spec §B6) -- this
   is known upfront (a constant of the Heimdall instance this Pipeline was
   built with), so it costs nothing to include before the cache is even
   checked. Every VERIFIED pass (cache hit, fresh, or rederived route)
   rewards each citation's importance via skuld.reward_pass() -- spec §B6:
   "pass -> each cited fact weight += 0.5" makes no exception for route.

6. `stats` (observability.Stats, spec §B8) is optional and defaults to
   None -- every `if self.stats:` call site is a one-line, side-effect-only
   counter bump, never a control-flow branch, so passing None costs
   nothing and changes no behavior. Route (cache/fresh/rederived) isn't
   persisted anywhere in the log, so cache_hits/cache_evicts/rederivations
   specifically need this live hook; everything else Stats tracks could in
   principle be reconstructed by replaying outcome.* events instead.
   `answer()`'s own wall-clock timing (started/derive_elapsed_ms, wrapped in
   a try/finally around the whole method) is this same kind of live-only
   signal: spec §A4's "total non-LLM overhead" budget is not the deriver's
   own self-reported Assertion.latency_ms (that's the LLM call, explicitly
   unbudgeted) and isn't reconstructable from the log after the fact either.

7. A read must never block. Two independent, composed defenses: (a) a
   candidate whose own remembered text contradicts its own extracted value
   (heimdall.GroundTruthFact.self_supported, computed once by
   Runtime.remember() at write time) is rejected BEFORE any derive() call
   is attempted -- UNSUPPORTED_BY_SOURCE, logged the same way every other
   verification failure is (_fail_self_contradiction), so a poisoned fact
   fails the exact same way, instantly, on every future read instead of
   repeating a derive+verify round trip that can only ever reach the same
   contradiction again. (b) every derive() call this method DOES make is
   still wrapped in `_derive_with_timeout` -- a hard wall-clock backstop
   (self.answer_timeout_s) independent of whatever timeout the deriver
   itself claims to enforce, for any OTHER way a derive() call might hang.
   Past that ceiling: abstain VERIFY_TIMEOUT, never wait longer.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from orlog.errors import VerifyTimeoutError
from orlog.heimdall import Heimdall, VerificationFailure, VerificationResult
from orlog.huginn import Deriver, DeriverFailure
from orlog.muninn import RouteCache, cache_key
from orlog.observability import Stats
from orlog.retrieval import retrieve_current_fact
from orlog.skuld import DEFAULT_PASS_BONUS, OutcomeLedger
from orlog.states import EpistemicState, transition
from orlog.verdandi import OPEN_VALID_TO, ProjectionVersion, SupersessionChainsView

EMPTY_EXCLUDED = {"by_validity": 0, "by_k": 0}
DEFAULT_ANSWER_TIMEOUT_S = 45.0


class AnswerCitation(BaseModel):
    """spec/ORLOG-SPEC.md §A3, the citation shape inside an Answer (richer
    than Assertion.citations' bare event ids: excerpt + validity window)."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    excerpt: str
    valid_from: datetime
    valid_to: datetime | None  # None == open-ended


class AnswerCandidate(BaseModel):
    """One of several equally-plausible (entity, attribute) keys a free-text
    recall() query might have meant -- only populated when reasons includes
    "AMBIGUOUS" (server_tools.recall_tool's semantic-fallback path via
    retrieval_hybrid.resolve_key()). Never a substitute for a verified
    answer: the caller is expected to disambiguate and recall() again with
    the exact key, not treat any one of these as the answer.
    """

    model_config = ConfigDict(extra="forbid")

    entity: str
    attribute: str
    entity_label: str | None
    entity_detail: str | None
    excerpt: str
    score: float


class Answer(BaseModel):
    """spec/ORLOG-SPEC.md §A3, the MCP-facing recall response."""

    model_config = ConfigDict(extra="forbid")

    verified: bool
    claim: str | None
    citations: list[AnswerCitation]
    as_of: datetime
    route: str | None  # "fresh" | "cache" | "rederived"; None when abstained
    truth_version: str | None
    abstained: bool
    reasons: list[str]
    excluded: dict[str, int]
    candidates: list[AnswerCandidate] = Field(default_factory=list)


class Pipeline:
    """Bundles one query-able snapshot: a verdandi view + its version, the
    events it was built from (for retrieval's candidate content), a heimdall
    verifier (its own independent ground truth), a muninn cache, a huginn
    deriver, and a skuld ledger.
    """

    def __init__(
        self,
        *,
        view: SupersessionChainsView,
        events_by_id: dict,
        projection_version: ProjectionVersion,
        cache: RouteCache,
        deriver: Deriver,
        verifier: Heimdall,
        ledger: OutcomeLedger,
        pass_bonus: float = DEFAULT_PASS_BONUS,
        stats: Stats | None = None,
        answer_timeout_s: float = DEFAULT_ANSWER_TIMEOUT_S,
    ) -> None:
        self.view = view
        self.events_by_id = events_by_id
        self.projection_version = projection_version
        self.cache = cache
        self.deriver = deriver
        self.verifier = verifier
        self.ledger = ledger
        self.pass_bonus = pass_bonus
        self.stats = stats
        self.answer_timeout_s = answer_timeout_s

    def answer(self, query_id: str, entity: str, attribute: str, as_of: datetime, *, now: datetime, k: int = 1) -> Answer:
        # Non-LLM latency (spec §A4/§B8: "total non-LLM overhead", explicitly
        # excluding the LLM call, which "is not budgeted here") is measured
        # directly as this method's own wall-clock time MINUS whatever time
        # was spent inside derive() calls -- not read off Assertion.latency_ms
        # (that field is the deriver's own self-reported latency, i.e. the
        # LLM call itself, and is None whenever ScriptedDeriver/no LLM is in
        # play, which is exactly why p50_ms/p95_ms used to stay null). The
        # try/finally covers every return path -- cache/fresh/rederived
        # serves and every abstain -- so the sample reflects real traffic,
        # not just successful serves.
        started = time.perf_counter()
        derive_elapsed_ms = 0.0
        try:
            if self.stats:
                self.stats.record_recall()
            query = f"{entity}.{attribute}"
            key = cache_key(
                query, as_of.isoformat(),
                {self.projection_version.projection: self.projection_version.version},
                self.verifier.truth_version,
            )

            # -- cached route: a hit is NEVER served without a fresh verify --
            cached = self.cache.get(key)
            if cached is not None:
                result = self._verify_and_log(cached.assertion, as_of, now)
                if result.status == "pass":
                    transition(EpistemicState.ASSERTED, EpistemicState.VERIFIED)  # then REINFORCED, VERIFIED's only legal next state
                    self.ledger.reward_pass(cached.assertion.citations, bonus=self.pass_bonus, now=now)
                    self.cache.touch(key, now=now)
                    if self.stats:
                        self.stats.record_cache_hit()
                    return self._served(cached.assertion, as_of, route="cache", truth_version=result.truth_version)
                self.cache.evict(key)  # invalidated by verification, never TTL
                if self.stats:
                    self.stats.record_cache_evict()
                # fall through to fresh derivation

            retrieval = retrieve_current_fact(entity, attribute, as_of, self.view, self.events_by_id, k=k)
            if not retrieval.candidates:
                return self._abstain(query_id, ["NO_CANDIDATES"], retrieval.excluded, as_of, truth_version=None, now=now)

            # -- write-time self-consistency pre-check (runtime.py's module
            # docstring point 4): a candidate whose own remembered text doesn't
            # support its own extracted value is a fail, not a puzzle for the
            # deriver to reconcile. No derive() call is attempted for it -- that
            # would spend a full (and for a real LLM deriver, potentially
            # network-bound) derive+verify round trip only to fail the exact
            # same way again on every future read. If every candidate is
            # self-contradictory, fail fast now instead.
            supported = [c for c in retrieval.candidates if self._is_self_supported(c.event_id)]
            if not supported:
                result = self._fail_self_contradiction(query_id, retrieval.candidates, now=now)
                return self._abstain(query_id, ["UNSUPPORTED_BY_SOURCE"], retrieval.excluded, as_of, truth_version=result.truth_version, now=now)

            derive_started = time.perf_counter()
            try:
                assertion = self._derive_with_timeout(query, as_of, supported, query_id=query_id, derived_at=now, route="fresh")
            except DeriverFailure as exc:
                return self._abstain(query_id, [exc.reason], retrieval.excluded, as_of, truth_version=None, now=now)
            except VerifyTimeoutError:
                return self._abstain(query_id, ["VERIFY_TIMEOUT"], retrieval.excluded, as_of, truth_version=None, now=now)
            finally:
                derive_elapsed_ms += (time.perf_counter() - derive_started) * 1000
            self._record_deriver_stats(assertion)
            result = self._verify_and_log(assertion, as_of, now)
            if result.status == "pass":
                transition(EpistemicState.ASSERTED, EpistemicState.VERIFIED)  # then REINFORCED, VERIFIED's only legal next state
                self.ledger.reward_pass(assertion.citations, bonus=self.pass_bonus, now=now)
                self.cache.put(key, assertion, now=now)
                return self._served(assertion, as_of, route="fresh", truth_version=result.truth_version, excluded=retrieval.excluded)

            # -- exactly one re-derivation (T3), pre-filtered by V2+V3 --
            bad_ids = {f.citation for f in result.failures}
            valid = [
                c for c in retrieval.candidates
                if c.event_id not in bad_ids and self.verifier.would_pass_validity(c.event_id, as_of)
            ]
            if not valid:
                transition(EpistemicState.ASSERTED, EpistemicState.ABSTAINED)
                return self._abstain(query_id, [f.code for f in result.failures], retrieval.excluded, as_of, truth_version=result.truth_version, now=now)

            if self.stats:
                self.stats.record_rederivation()
            derive2_started = time.perf_counter()
            try:
                assertion2 = self._derive_with_timeout(query, as_of, valid, query_id=query_id, derived_at=now, route="rederived")
            except DeriverFailure as exc:
                return self._abstain(query_id, [exc.reason], retrieval.excluded, as_of, truth_version=None, now=now)
            except VerifyTimeoutError:
                return self._abstain(query_id, ["VERIFY_TIMEOUT"], retrieval.excluded, as_of, truth_version=None, now=now)
            finally:
                derive_elapsed_ms += (time.perf_counter() - derive2_started) * 1000
            self._record_deriver_stats(assertion2)
            result2 = self._verify_and_log(assertion2, as_of, now)
            if result2.status == "pass":
                transition(EpistemicState.ASSERTED, EpistemicState.VERIFIED)  # then REINFORCED, VERIFIED's only legal next state
                self.ledger.reward_pass(assertion2.citations, bonus=self.pass_bonus, now=now)
                self.cache.put(key, assertion2, now=now)
                return self._served(assertion2, as_of, route="rederived", truth_version=result2.truth_version, excluded=retrieval.excluded)

            transition(EpistemicState.ASSERTED, EpistemicState.ABSTAINED)
            return self._abstain(query_id, [f.code for f in result2.failures], retrieval.excluded, as_of, truth_version=result2.truth_version, now=now)
        finally:
            if self.stats:
                total_ms = (time.perf_counter() - started) * 1000
                self.stats.record_latency_ms(max(0.0, total_ms - derive_elapsed_ms))

    def _verify_and_log(self, assertion, as_of: datetime, now: datetime):
        """verify() + stats + ledger, in the one order every call site in
        answer() needs -- factored out so the fail-path ledger write
        (skuld.py: "every completed verification, pass or fail") can't be
        forgotten at one site while present at another, same as the bug
        this method fixes.
        """
        result = self.verifier.verify(assertion, as_of, checked_at=now)
        self._record_verification_stats(result)
        self.ledger.record_verification(result)
        return result

    def _derive_with_timeout(self, query, as_of, candidates, *, query_id, derived_at, route):
        """Runs self.deriver.derive() on a worker thread with a hard
        wall-clock ceiling (self.answer_timeout_s) -- a backstop independent
        of whatever per-call timeout the deriver itself claims to enforce
        (DeriverConfig.timeout_s / huginn_llm.LLMDeriver's own timeout_s), so
        a read never blocks indefinitely even if that inner timeout fails to
        fire. Mirrors runtime.py's embedder-build watchdog: a plain daemon
        thread, not a ThreadPoolExecutor (whose own shutdown/atexit joining
        would defeat the point of "abandon a hung call, don't wait on it").
        Raises VerifyTimeoutError past the ceiling; DeriverFailure and any
        other exception the deriver itself raises are re-raised unchanged on
        the caller's thread.
        """
        outcome: dict = {}

        def _run() -> None:
            try:
                outcome["assertion"] = self.deriver.derive(query, as_of, candidates, query_id=query_id, derived_at=derived_at, route=route)
            except Exception as exc:  # re-raised on the caller's thread below
                outcome["error"] = exc

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=self.answer_timeout_s)
        if thread.is_alive():
            raise VerifyTimeoutError(f"derive() did not complete within {self.answer_timeout_s}s for query_id={query_id!r}")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["assertion"]

    def _is_self_supported(self, event_id: str) -> bool:
        """heimdall.GroundTruthFact.self_supported, defaulting to True (not
        poisoned) if this verifier has no opinion -- e.g. a test double that
        only implements the subset of the Verifier surface its scenario
        actually needs.
        """
        fact = self.verifier.fact(event_id) if hasattr(self.verifier, "fact") else None
        return fact is None or fact.self_supported

    def _fail_self_contradiction(self, query_id: str, candidates, *, now: datetime) -> VerificationResult:
        """Synthesizes and logs a VerificationResult for a set of candidates
        that are ALL self-contradictory (Runtime.remember()'s write-time
        self_supported verdict) -- logged and counted exactly like every
        other verification failure (_verify_and_log), even though no
        Assertion was ever derived to trigger it. Deriving one here is
        precisely the wasted (and, for a real LLM deriver, potentially
        network-bound) round trip this check exists to skip.
        """
        result = VerificationResult(
            assertion_query_id=query_id,
            status="fail",
            checked_at=now,
            truth_version=self.verifier.truth_version,
            failures=[
                VerificationFailure(citation=c.event_id, code="UNSUPPORTED_BY_SOURCE", detail="fact's own remembered text does not support its extracted value")
                for c in candidates
            ],
        )
        self._record_verification_stats(result)
        self.ledger.record_verification(result)
        return result

    def _citations_from(self, assertion) -> list[AnswerCitation]:
        citations = []
        for event_id in assertion.citations:
            fact = self.verifier.fact(event_id)
            if fact is None:
                continue  # defensive only: a served assertion's citations already passed V2
            citations.append(
                AnswerCitation(
                    event_id=event_id,
                    excerpt=fact.evidence_span if fact.evidence_span is not None else fact.value,
                    valid_from=fact.valid_from,
                    valid_to=None if fact.valid_to == OPEN_VALID_TO else fact.valid_to,
                )
            )
        return citations

    def _served(self, assertion, as_of: datetime, *, route: str, truth_version: str, excluded: dict | None = None) -> Answer:
        return Answer(
            verified=True,
            claim=assertion.claim,
            citations=self._citations_from(assertion),
            as_of=as_of,
            route=route,
            truth_version=truth_version,
            abstained=False,
            reasons=[],
            excluded=dict(excluded) if excluded is not None else dict(EMPTY_EXCLUDED),
        )

    def _record_deriver_stats(self, assertion) -> None:
        # Only tokens, not assertion.latency_ms -- that field is the
        # deriver's own (LLM) latency, a separate metric from the non-LLM
        # overhead answer() measures and records itself (see answer()'s own
        # comment); mixing the two into stats.p50_ms/p95_ms would make that
        # number mean neither thing.
        if self.stats:
            self.stats.record_tokens(assertion.tokens)

    def _record_verification_stats(self, result) -> None:
        if not self.stats:
            return
        if result.status == "pass":
            self.stats.record_verification_pass()
        else:
            self.stats.record_verification_fail([f.code for f in result.failures])

    def _abstain(
        self, query_id: str, reasons: list[str], excluded: dict, as_of: datetime, *,
        truth_version: str | None, now: datetime, candidates: list[AnswerCandidate] | None = None,
    ) -> Answer:
        self.ledger.record_abstention(query_id=query_id, reasons=reasons, occurred_at=now)
        if self.stats:
            self.stats.record_abstention(reasons)
        return Answer(
            verified=False,
            claim=None,
            citations=[],
            as_of=as_of,
            route=None,
            truth_version=truth_version,
            abstained=True,
            reasons=reasons,
            excluded=dict(excluded),
            candidates=list(candidates) if candidates is not None else [],
        )
