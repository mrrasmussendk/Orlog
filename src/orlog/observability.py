"""observability: structured logs + stats counters (ORLOG-SPEC.md §B8).

Design decisions:

1. StructuredLogger writes one JSON object per line, and nothing else --
   spec §B8: "structured logs (JSON lines, no PII)". It has no PII-scrubbing
   logic of its own; that's scrub.py's job, upstream, before any text ever
   reaches a log call here. Callers must never pass raw user text as a
   field value.

2. Stats is a plain in-memory counter set, not persisted -- `orlog stats`
   (the CLI) and the stats MCP resource are both meant to reflect the
   CURRENT process's activity, matching spec §B8's own list: appends,
   recalls, cache_hits, cache_evicts, verifications{pass,fail by code},
   abstentions{by reason}, rederivations, tokens{in,out}, p50/p95 non-LLM
   latency. Percentiles use simple nearest-rank interpolation over
   in-memory samples -- adequate for a reference implementation, not a
   streaming/production-grade percentile estimator.

3. Pipeline.answer() takes an optional `stats: Stats | None` and calls a
   handful of small record_*() methods at the relevant points (see
   pipeline.py) -- most outcomes are already derivable by replaying the
   log's outcome.* events, but route (cache/fresh/rederived) is NOT
   persisted anywhere, so cache_hits/cache_evicts/rederivations need a
   live hook rather than being reconstructable after the fact.
"""

from __future__ import annotations

import json
import sys
import time
from collections import deque
from typing import Any, TextIO


class StructuredLogger:
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def log(self, event: str, **fields: Any) -> None:
        record = {"ts": time.time(), "event": event, **fields}
        self._stream.write(json.dumps(record, sort_keys=True) + "\n")
        self._stream.flush()


#: How many recent latency samples to keep for p50/p95. A trailing
#: window, not the whole history: see Stats.record_latency_ms.
LATENCY_SAMPLE_WINDOW = 10_000


def _percentile(ordered: list[float], p: float) -> float | None:
    """`ordered` must already be sorted ascending -- snapshot() sorts once
    and reads several percentiles off the result, rather than re-sorting
    the whole sample window for each one.
    """
    if not ordered:
        return None
    index = min(len(ordered) - 1, int(round(p * (len(ordered) - 1))))
    return ordered[index]


class Stats:
    def __init__(self) -> None:
        self.appends = 0
        self.recalls = 0
        self.cache_hits = 0
        self.cache_evicts = 0
        self.rederivations = 0
        self._verification_pass = 0
        self._verification_fail_by_code: dict[str, int] = {}
        self._abstentions_by_reason: dict[str, int] = {}
        self._tokens_in = 0
        self._tokens_out = 0
        self._latency_samples_ms: deque[float] = deque(maxlen=LATENCY_SAMPLE_WINDOW)

    def record_append(self) -> None:
        self.appends += 1

    def record_recall(self) -> None:
        self.recalls += 1

    def record_cache_hit(self) -> None:
        self.cache_hits += 1

    def record_cache_evict(self) -> None:
        self.cache_evicts += 1

    def record_rederivation(self) -> None:
        self.rederivations += 1

    def record_verification_pass(self) -> None:
        self._verification_pass += 1

    def record_verification_fail(self, codes: list[str]) -> None:
        for code in codes:
            self._verification_fail_by_code[code] = self._verification_fail_by_code.get(code, 0) + 1

    def record_abstention(self, reasons: list[str]) -> None:
        for reason in reasons:
            self._abstentions_by_reason[reason] = self._abstentions_by_reason.get(reason, 0) + 1

    def record_tokens(self, tokens: dict[str, int] | None) -> None:
        if tokens:
            self._tokens_in += tokens.get("in", 0)
            self._tokens_out += tokens.get("out", 0)

    def record_latency_ms(self, latency_ms: float) -> None:
        # Bounded, because `orlog serve` is a long-lived process and this
        # gets one float per recall forever. An unbounded list grew without
        # limit for the life of the server AND made every stats read
        # progressively slower, since snapshot() sorts the whole thing twice
        # (once each for p50 and p95). A trailing window is also the more
        # useful statistic: recent latency, not a lifetime average that
        # can never move.
        self._latency_samples_ms.append(latency_ms)

    def snapshot(self) -> dict[str, Any]:
        latencies = sorted(self._latency_samples_ms)  # sorted once, not once per percentile
        return {
            "appends": self.appends,
            "recalls": self.recalls,
            "cache_hits": self.cache_hits,
            "cache_evicts": self.cache_evicts,
            "verifications": {"pass": self._verification_pass, "fail": dict(self._verification_fail_by_code)},
            "abstentions": {"by_reason": dict(self._abstentions_by_reason)},
            "rederivations": self.rederivations,
            "tokens": {"in": self._tokens_in, "out": self._tokens_out},
            "p50_ms": _percentile(latencies, 0.50),
            "p95_ms": _percentile(latencies, 0.95),
        }
