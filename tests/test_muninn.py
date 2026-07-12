"""Contract: cache_key includes truth_version (ORLOG-SPEC.md §B6), and
RouteCache evicts least-recently-used entries once max_entries is exceeded
-- a storage bound, not a trust mechanism.
"""

from datetime import datetime, timezone

from orlog.huginn import Assertion
from orlog.muninn import RouteCache, cache_key

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _assertion(n: int) -> Assertion:
    return Assertion(query_id=f"q{n}", claim=f"claim {n}", citations=[f"ev-{n}"], derived_by="test", derived_at=NOW, route="fresh")


def test_cache_key_changes_when_truth_version_changes():
    key_a = cache_key("q", "now", {"p": 1}, "truth-a")
    key_b = cache_key("q", "now", {"p": 1}, "truth-b")
    assert key_a != key_b


def test_cache_key_is_stable_for_identical_inputs():
    key_a = cache_key("q", "now", {"p": 1}, "truth-a")
    key_b = cache_key("q", "now", {"p": 1}, "truth-a")
    assert key_a == key_b


def test_lru_eviction_drops_the_least_recently_used_entry():
    cache = RouteCache(max_entries=2)
    cache.put("a", _assertion(1), now=NOW)
    cache.put("b", _assertion(2), now=NOW)
    cache.put("c", _assertion(3), now=NOW)  # evicts "a" (never touched since)

    assert cache.get("a") is None
    assert cache.get("b") is not None
    assert cache.get("c") is not None
    assert len(cache) == 2


def test_get_counts_as_recently_used_and_protects_from_eviction():
    cache = RouteCache(max_entries=2)
    cache.put("a", _assertion(1), now=NOW)
    cache.put("b", _assertion(2), now=NOW)
    cache.get("a")  # "a" is now more recently used than "b"
    cache.put("c", _assertion(3), now=NOW)  # evicts "b", not "a"

    assert cache.get("a") is not None
    assert cache.get("b") is None
    assert cache.get("c") is not None


def test_eviction_by_lru_is_not_the_same_as_invalidation():
    # An LRU-evicted key is just a cold-cache miss on the next lookup --
    # nothing marks it "invalid", it simply isn't there anymore.
    cache = RouteCache(max_entries=1)
    cache.put("a", _assertion(1), now=NOW)
    cache.put("b", _assertion(2), now=NOW)

    assert cache.get("a") is None
    assert len(cache) == 1
