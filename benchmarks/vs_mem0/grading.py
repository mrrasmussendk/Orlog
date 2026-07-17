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
