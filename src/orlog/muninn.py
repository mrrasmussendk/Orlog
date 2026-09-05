"""muninn: the route cache -- memory, re-checked on every return
(ORLOG-SPEC.md §3.7, cache semantics in §5/§B6).

Design decisions:

1. This module deliberately does NOT re-verify anything itself -- that
   stays in pipeline.py's answer(), the only place spec §5's "a cache hit
   is never served without a fresh heimdall.verify" rule can be enforced
   end to end. RouteCache is just storage: get/put/touch/evict.

2. cache_key follows spec §B6's fuller definition (superseding §3.7's
   draft version): SHA-256 of canonical(query_class, as_of_class,
   projection_versions, truth_version). Including truth_version means a
   sovereign-truth refresh (heimdall rebuilt from a changed log, even
   before verdandi re-projects) also gets its own cache key -- on top of
   the re-verification safety net pipeline.py already provides on every
   hit, this is a second, independent invalidation path.

3. `max_entries` (default 10,000, spec §B6) triggers LRU eviction once
   exceeded -- explicitly a STORAGE bound, not a trust mechanism (spec §5:
   "TTL MAY exist only as a storage bound, not a trust mechanism" applies
   the same way here). An LRU-evicted entry is gone, full stop -- it is
   NOT "invalidated" in the trust sense; a query for it after eviction is
   just a cold-cache miss, indistinguishable from one that was never
   cached, and goes through fresh derivation + verification like any miss.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from orlog.huginn import Assertion

DEFAULT_MAX_ENTRIES = 10_000


def cache_key(query_class: str, as_of_class: str, projection_versions: dict[str, int], truth_version: str | None = None) -> str:
    """spec/ORLOG-SPEC.md §B6: SHA-256 of canonical(query_class, as_of_class,
    projection_versions, truth_version).
    """
    canonical = json.dumps(
        {
            "query_class": query_class,
            "as_of_class": as_of_class,
            "projection_versions": projection_versions,
            "truth_version": truth_version,
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CacheEntry(BaseModel):
    """spec/ORLOG-SPEC.md §3.7."""

    model_config = ConfigDict(extra="forbid")

    key: str
    assertion: Assertion
    created_at: datetime
    last_verified_at: datetime
    hits: int = 0
    #: The retrieval exclusion counts that produced this assertion. Stored
    #: so a cache hit reports the same `excluded` as the fresh answer did:
    #: serving the hardcoded zeros meant the identical question answered
    #: twice reported "1 superseded revision excluded" and then "0", and a
    #: client using `excluded` to decide whether to call recall_history was
    #: told there was no history to look at.
    excluded: dict = {}


class RouteCache:
    """An in-memory, LRU-bounded dict of CacheEntry, keyed by cache_key().
    No TTL -- only the max_entries storage bound and heimdall-driven
    eviction (via pipeline.py calling evict() on a failed re-verification).
    """

    def __init__(self, *, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self._store: "OrderedDict[str, CacheEntry]" = OrderedDict()
        self._max_entries = max_entries

    def get(self, key: str) -> CacheEntry | None:
        entry = self._store.get(key)
        if entry is not None:
            self._store.move_to_end(key)  # most-recently-used
        return entry

    def put(self, key: str, assertion: Assertion, *, now: datetime, excluded: dict | None = None) -> CacheEntry:
        entry = CacheEntry(
            key=key, assertion=assertion, created_at=now, last_verified_at=now, hits=0,
            excluded=dict(excluded or {}),
        )
        self._store[key] = entry
        self._store.move_to_end(key)
        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)  # evict least-recently-used
        return entry

    def touch(self, key: str, *, now: datetime) -> None:
        entry = self._store.get(key)
        if entry is not None:
            self._store[key] = entry.model_copy(update={"last_verified_at": now, "hits": entry.hits + 1})
            self._store.move_to_end(key)

    def evict(self, key: str) -> None:
        self._store.pop(key, None)

    def __len__(self) -> int:
        return len(self._store)
