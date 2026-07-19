"""grading.py: pure, deterministic scoring helpers -- no I/O, no network."""

from __future__ import annotations

import math


def is_correct(returned_text: str | None, expected_value: str) -> bool:
    """Case-insensitive substring match. False for None/empty returned_text."""
    if not returned_text:
        return False
    return expected_value.lower() in returned_text.lower()


def percentiles(values_ms: list[float]) -> dict:
    """Nearest-rank mean/p50/p95 over values_ms. Empty input -> all None,
    n=0. Nearest-rank index for percentile p (0-100) over n sorted values:
    index = ceil(p / 100 * n) - 1, clamped to [0, n-1].
    """
    if not values_ms:
        return {"mean": None, "p50": None, "p95": None, "n": 0}
    ordered = sorted(values_ms)
    n = len(ordered)

    def _rank(pct: float) -> float:
        index = max(0, min(n - 1, math.ceil(pct / 100 * n) - 1))
        return ordered[index]

    return {
        "mean": sum(ordered) / n,
        "p50": _rank(50),
        "p95": _rank(95),
        "n": n,
    }


def summarize_runs(values: list[float | None]) -> dict:
    """Median/min/max across N per-run point estimates of the same metric
    (e.g. N runs' query-latency means, or N runs' accuracy rates). None
    values (metric unavailable that run) are dropped before aggregating.
    Empty/all-None input -> all None, n=0.
    """
    present = [v for v in values if v is not None]
    if not present:
        return {"median": None, "min": None, "max": None, "n": 0}
    ordered = sorted(present)
    n = len(ordered)
    mid = n // 2
    median = ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return {"median": median, "min": ordered[0], "max": ordered[-1], "n": n}
