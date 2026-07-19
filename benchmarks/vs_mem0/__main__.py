"""Runs the full orlog vs Mem0 benchmark end to end: both systems' raw
runs, then scoring. Requires both ANTHROPIC_API_KEY (orlog) and OPENAI_API_KEY
(Mem0) in the environment; makes real, billed API calls to both providers
for every ingest and query -- see README.md.

Pass --runs N (default 3) to repeat the whole benchmark N times and report
median/min/max per metric instead of a single-sample point estimate. This
matters most for latency: a 3-run check found orlog's and Mem0's query
latency swinging 40-180% run to run from ordinary API-side variance, while
accuracy/abstention held stable within about one question's worth of noise
-- see README.md's "Run-to-run variance" section. Each run costs roughly
what README.md's cost estimate says (well under $1), so N=3 is a few
dollars total, not N=1's fraction of one.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from benchmarks.vs_mem0 import run_mem0, run_orlog, score

BENCH_DIR = Path(__file__).parent
RUNS_DIR = BENCH_DIR / "runs"


def main(n_runs: int = 3) -> None:
    missing = [
        name for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
        if not os.environ.get(name)
    ]
    if missing:
        print(f"{', '.join(missing)} not set -- orlog uses Anthropic, mem0 uses OpenAI.", file=sys.stderr)
        raise SystemExit(1)

    RUNS_DIR.mkdir(exist_ok=True)
    orlog_paths, mem0_paths = [], []
    for i in range(n_runs):
        print(f"Run {i + 1}/{n_runs}: orlog...")
        orlog_path = RUNS_DIR / f"orlog_results_run{i}.json"
        run_orlog.main(orlog_path)
        print(f"Run {i + 1}/{n_runs}: mem0...")
        mem0_path = RUNS_DIR / f"mem0_results_run{i}.json"
        run_mem0.main(mem0_path)
        orlog_paths.append(orlog_path)
        mem0_paths.append(mem0_path)

    print("Scoring...")
    combined = score.main_multi(orlog_paths, mem0_paths, BENCH_DIR / "results.json")
    print(f"Wrote {BENCH_DIR / 'results.json'} ({n_runs} run(s))")
    for system in ("orlog", "mem0"):
        m = combined[system]
        print(
            f"{system}: accuracy(median)={m['accuracy']['overall']['median']} "
            f"abstention(median)={m['abstention']['correct_rate']['median']} "
            f"query_p50_ms(median)={m['query_known']['p50']['median']}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--runs", type=int, default=3,
        help="Number of independent full runs to aggregate (default 3; each makes real billed API calls).",
    )
    args = parser.parse_args()
    main(n_runs=args.runs)
