"""Phase 0 before/after benchmark: what the §B12 seams cost and save.

Measures the operations Phase 0 touched -- ingest, and the five read tools
that used to re-read the whole log on every call -- at several workspace
sizes, through the SAME public API on both sides, so the script runs
unmodified against a pre-Phase-0 checkout and a post-Phase-0 one.

    python -m benchmarks.phase0.bench --out results-after.json

Deliberately uses only the tool functions in orlog.server_tools, never the
index or the registries directly: the question is what a caller sees, not
whether an internal got faster. The deriver is the ScriptedDeriver and the
embedder is "hashing", so there are no API calls and no model download --
every number here is orlog's own machinery, not a provider's latency.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

from orlog.config import OrlogConfig, RetrievalConfig, WorkspaceConfig
from orlog.runtime import Runtime
from orlog.server_tools import (
    check_action_tool,
    list_attributes_tool,
    list_entities_tool,
    recall_history_tool,
    recall_tool,
    remember_tool,
    stats_tool,
)
from orlog.vault import generate_key
from orlog.workspace import Workspace

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
ENTITIES = 50
ATTRIBUTES = 8


def _percentile(samples: list[float], p: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(round(p * (len(ordered) - 1))))]


def _summarize(samples: list[float]) -> dict:
    return {
        "n": len(samples),
        "p50_ms": round(_percentile(samples, 0.50), 3),
        "p95_ms": round(_percentile(samples, 0.95), 3),
        "mean_ms": round(statistics.fmean(samples), 3),
    }


def _measure(call, runs: int) -> list[float]:
    call()  # warm
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1000)
    return samples


def run_scale(n_events: int, *, query_runs: int) -> dict:
    os.environ.setdefault("ORLOG_VAULT_KEY", generate_key())
    root = tempfile.mkdtemp(prefix=f"orlog-bench-{n_events}-")
    runtime = Runtime(
        Workspace(root),
        OrlogConfig(
            workspace=WorkspaceConfig(name="bench"),
            retrieval=RetrievalConfig(embedder="hashing"),
        ),
    )
    try:
        # -- ingest ------------------------------------------------------
        started = time.perf_counter()
        for i in range(n_events):
            entity = f"user:{i % ENTITIES}"
            attribute = f"attr_{(i // ENTITIES) % ATTRIBUTES}"
            value = f"v{i}"
            remember_tool(
                runtime,
                f"{entity} {attribute} is {value}",
                occurred_at=(T0 + timedelta(seconds=i)).isoformat(),
                entity=entity,
                attribute=attribute,
                value=value,
            )
        ingest_s = time.perf_counter() - started

        # -- reads -------------------------------------------------------
        results = {
            "events": n_events,
            "ingest": {
                "total_s": round(ingest_s, 3),
                "per_event_ms": round(ingest_s * 1000 / n_events, 3),
                "events_per_s": round(n_events / ingest_s, 1),
            },
            "recall": _summarize(_measure(lambda: recall_tool(runtime, "user:7.attr_0"), query_runs)),
            "recall_history": _summarize(
                _measure(lambda: recall_history_tool(runtime, "user:7.attr_0"), query_runs)
            ),
            "list_entities": _summarize(
                _measure(lambda: list_entities_tool(runtime, limit=100), query_runs)
            ),
            "list_attributes": _summarize(
                _measure(lambda: list_attributes_tool(runtime, "user:7"), query_runs)
            ),
            "check_action": _summarize(
                _measure(lambda: check_action_tool(runtime, "user:7.attr_0"), query_runs)
            ),
        }
        results["stats"] = stats_tool(runtime)
        return results
    finally:
        runtime.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.phase0.bench")
    parser.add_argument("--scales", default="250,1000,2500",
                        help="comma-separated workspace sizes in events")
    parser.add_argument("--query-runs", type=int, default=30)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    scales = [int(s) for s in args.scales.split(",") if s.strip()]
    report = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "orlog_has_index_query_api": _has_index_query_api(),
        "scales": [],
    }
    for n in scales:
        print(f"-- {n} events ...", file=sys.stderr, flush=True)
        scale = run_scale(n, query_runs=args.query_runs)
        report["scales"].append(scale)
        print(
            f"   ingest {scale['ingest']['total_s']}s "
            f"({scale['ingest']['events_per_s']}/s)  "
            f"list_entities p50 {scale['list_entities']['p50_ms']}ms  "
            f"recall p50 {scale['recall']['p50_ms']}ms",
            file=sys.stderr, flush=True,
        )

    text = json.dumps(report, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


def _has_index_query_api() -> bool:
    """Which side of Phase 0 this checkout is on -- recorded in the report
    so two result files can never be mislabelled."""
    try:
        import orlog.queries  # noqa: F401
    except ImportError:
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
