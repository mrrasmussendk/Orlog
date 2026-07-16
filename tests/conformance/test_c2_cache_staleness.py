"""Conformance cell C2: cache staleness (ORLOG-SPEC.md §6).

PASS criterion (spec table): 0 stale answers served; every invalidated
route is re-derived or abstained; >=1 still-valid route is served from
cache (proving reuse works).

Three scenarios, matching the independent invalidation paths spec §B6
gives the cache:

1. Reuse within a stable truth_version: a second identical query is served
   straight from cache (no truth/projection change happened).

2. A sovereign-truth refresh (truth_version changes, spec §B6: "eviction on
   verification failure OR projection/truth version change") rotates the
   cache key for EVERYTHING, not just the specific fact that changed --
   heimdall re-derives its ground truth independently of, and possibly
   ahead of, the (unchanged) projection. Every query after the refresh is
   therefore a cache miss, but still comes back correct: the changed fact
   abstains (its only citable evidence is now expired and gets pre-filtered
   out before the one permitted re-derivation), and the unchanged fact is
   still answered correctly via fresh re-derivation. 0 stale answers are
   served either way -- reuse is a performance property, not a trust one.

3. A per-key verification-failure eviction: a cached assertion that fails
   re-verification is evicted on its own key alone, independent of the
   truth_version-rotation path in scenario 2 -- an untouched neighbor key
   is still served straight from cache while the changed key is re-derived.
"""

from datetime import datetime, timezone

from orlog.heimdall import Heimdall, build_ground_truth
from orlog.huginn import ScriptedDeriver
from orlog.muninn import RouteCache
from orlog.pipeline import Pipeline
from orlog.skuld import OutcomeLedger
from orlog.verdandi import build_supersession_chains

T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 6, 1, tzinfo=timezone.utc)
T_BETWEEN = datetime(2026, 3, 1, tzinfo=timezone.utc)


def _build_pipeline(log, cache):
    events = log.read_all()
    view, pv = build_supersession_chains(events, builder="test", built_at=T1)
    truth = build_ground_truth(events)
    return Pipeline(
        view=view,
        events_by_id={e.id: e for e in events},
        projection_version=pv,
        cache=cache,
        deriver=ScriptedDeriver(),
        verifier=Heimdall(truth, truth_version="v-initial"),
        ledger=OutcomeLedger(log),
    )


def test_cache_reuse_works_when_nothing_has_changed(make_log, make_event):
    log = make_log()
    log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "status", "value": "active"}))

    pipeline = _build_pipeline(log, RouteCache())

    first = pipeline.answer("q1", "user:1", "status", T2, now=T1)
    second = pipeline.answer("q1b", "user:1", "status", T2, now=T1)

    assert first.verified is True and first.route == "fresh"
    assert second.verified is True and second.route == "cache"
    assert second.claim == "user:1.status = active"


def test_truth_refresh_prevents_a_stale_answer_from_ever_being_served(make_log, make_event):
    log = make_log()
    log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "status", "value": "active"}))
    log.append(make_event(occurred_at=T1, payload={"entity": "user:2", "attribute": "status", "value": "active"}))

    cache = RouteCache()
    pipeline = _build_pipeline(log, cache)

    # Round 1: both facts answered fresh and cached.
    r1 = pipeline.answer("q1", "user:1", "status", T2, now=T1)
    r2 = pipeline.answer("q2", "user:2", "status", T2, now=T1)
    assert r1.verified is True and r1.route == "fresh"
    assert r2.verified is True and r2.route == "fresh"

    # user:1's fact changes; user:2's does not. The projection is NOT
    # rebuilt (simulating "verdandi hasn't caught up yet"), but heimdall's
    # ground truth IS refreshed, with a new truth_version -- it is
    # sovereign and independent of L1.
    log.append(make_event(occurred_at=T2, payload={"entity": "user:1", "attribute": "status", "value": "inactive"}))
    events = log.read_all()
    pipeline.verifier = Heimdall(build_ground_truth(events), truth_version="v-refreshed")

    # user:1: the new truth_version means this is a fresh cache key (a
    # miss), so retrieval runs again -- but it can only re-cite the now-
    # expired fact, which is pre-filtered out before the one permitted
    # re-derivation, so the query abstains. It MUST NOT come back as a
    # verified "active" answer.
    r1_again = pipeline.answer("q1b", "user:1", "status", T2, now=T2)
    assert not (r1_again.verified is True and r1_again.claim == "user:1.status = active")
    assert r1_again.abstained is True

    # user:2: also a cache miss now (truth_version rotated every key), but
    # fresh re-derivation still finds the correct, unchanged answer -- 0
    # stale answers served, even though this particular route wasn't a
    # cache hit.
    r2_again = pipeline.answer("q2b", "user:2", "status", T2, now=T2)
    assert r2_again.verified is True
    assert r2_again.claim == "user:2.status = active"


def test_a_verification_failure_evicts_only_its_own_key_not_a_neighbors(make_log, make_event):
    # Distinct from the truth_version-rotation test above: this isolates
    # the OTHER invalidation path pipeline.answer() has (a cached
    # assertion that fails re-verification gets evicted on its own,
    # spec §B6) by rebuilding the verifier from new events while
    # deliberately keeping truth_version as the SAME literal string --
    # proving reuse is genuinely per-key, not merely "whatever wasn't
    # touched by the last truth_version bump."
    log = make_log()
    log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "status", "value": "active"}))
    log.append(make_event(occurred_at=T1, payload={"entity": "user:2", "attribute": "status", "value": "active"}))

    cache = RouteCache()
    pipeline = _build_pipeline(log, cache)

    r1 = pipeline.answer("q1", "user:1", "status", T2, now=T1)
    r2 = pipeline.answer("q2", "user:2", "status", T2, now=T1)
    assert r1.route == "fresh" and r2.route == "fresh"

    log.append(make_event(occurred_at=T_BETWEEN, payload={"entity": "user:1", "attribute": "status", "value": "inactive"}))
    events = log.read_all()
    view, pv = build_supersession_chains(events, builder="test", built_at=T1)
    pipeline.view = view
    pipeline.projection_version = pv
    pipeline.events_by_id = {e.id: e for e in events}
    pipeline.verifier = Heimdall(build_ground_truth(events), truth_version="v-initial")

    r1b = pipeline.answer("q1b", "user:1", "status", T2, now=T2)
    r2b = pipeline.answer("q2b", "user:2", "status", T2, now=T2)

    assert r1b.verified is True and r1b.claim == "user:1.status = inactive"  # re-derived, not stale
    assert r2b.route == "cache"  # untouched neighbor: still served straight from cache
    assert r2b.claim == "user:2.status = active"
