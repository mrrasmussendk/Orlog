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
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from orlog.heimdall import Heimdall
from orlog.huginn import Deriver, DeriverFailure
from orlog.muninn import RouteCache, cache_key
from orlog.observability import Stats
from orlog.retrieval import retrieve_current_fact
from orlog.skuld import DEFAULT_PASS_BONUS, OutcomeLedger
from orlog.states import EpistemicState, transition
from orlog.verdandi import OPEN_VALID_TO, ProjectionVersion, SupersessionChainsView

EMPTY_EXCLUDED = {"by_validity": 0, "by_k": 0}


class AnswerCitation(BaseModel):
    """spec/ORLOG-SPEC.md §A3, the citation shape inside an Answer (richer
    than Assertion.citations' bare event ids: excerpt + validity window)."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    excerpt: str
    valid_from: datetime
    valid_to: datetime | None  # None == open-ended


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

    def answer(self, query_id: str, entity: str, attribute: str, as_of: datetime, *, now: datetime, k: int = 1) -> Answer:
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
            result = self.verifier.verify(cached.assertion, as_of, checked_at=now)
            self._record_verification_stats(result)
            if result.status == "pass":
                transition(EpistemicState.ASSERTED, EpistemicState.VERIFIED)
                self.ledger.record_verification(result)
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

        try:
            assertion = self.deriver.derive(query, as_of, retrieval.candidates, query_id=query_id, derived_at=now, route="fresh")
        except DeriverFailure as exc:
            return self._abstain(query_id, [exc.reason], retrieval.excluded, as_of, truth_version=None, now=now)
        self._record_deriver_stats(assertion)
        result = self.verifier.verify(assertion, as_of, checked_at=now)
        self._record_verification_stats(result)
        if result.status == "pass":
            transition(EpistemicState.ASSERTED, EpistemicState.VERIFIED)
            self.ledger.record_verification(result)
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
        try:
            assertion2 = self.deriver.derive(query, as_of, valid, query_id=query_id, derived_at=now, route="rederived")
        except DeriverFailure as exc:
            return self._abstain(query_id, [exc.reason], retrieval.excluded, as_of, truth_version=None, now=now)
        self._record_deriver_stats(assertion2)
        result2 = self.verifier.verify(assertion2, as_of, checked_at=now)
        self._record_verification_stats(result2)
        if result2.status == "pass":
            transition(EpistemicState.ASSERTED, EpistemicState.VERIFIED)
            self.ledger.record_verification(result2)
            self.ledger.reward_pass(assertion2.citations, bonus=self.pass_bonus, now=now)
            self.cache.put(key, assertion2, now=now)
            return self._served(assertion2, as_of, route="rederived", truth_version=result2.truth_version, excluded=retrieval.excluded)

        transition(EpistemicState.ASSERTED, EpistemicState.ABSTAINED)
        return self._abstain(query_id, [f.code for f in result2.failures], retrieval.excluded, as_of, truth_version=result2.truth_version, now=now)

    def _citations_from(self, assertion) -> list[AnswerCitation]:
        citations = []
        for event_id in assertion.citations:
            fact = self.verifier.fact(event_id)
            if fact is None:
                continue  # defensive only: a served assertion's citations already passed V2
            citations.append(
                AnswerCitation(
                    event_id=event_id,
                    excerpt=fact.value,
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
        if self.stats:
            self.stats.record_tokens(assertion.tokens)
            if assertion.latency_ms is not None:
                self.stats.record_latency_ms(assertion.latency_ms)

    def _record_verification_stats(self, result) -> None:
        if not self.stats:
            return
        if result.status == "pass":
            self.stats.record_verification_pass()
        else:
            self.stats.record_verification_fail([f.code for f in result.failures])

    def _abstain(self, query_id: str, reasons: list[str], excluded: dict, as_of: datetime, *, truth_version: str | None, now: datetime) -> Answer:
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
        )
