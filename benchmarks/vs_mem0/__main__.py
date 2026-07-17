"""Runs the full orlog vs Mem0 benchmark end to end: both systems' raw
runs, then scoring. Requires both ANTHROPIC_API_KEY (orlog) and OPENAI_API_KEY
(Mem0) in the environment; makes real, billed API calls to both providers
for every ingest and query -- see README.md.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from benchmarks.vs_mem0 import run_mem0, run_orlog, score

BENCH_DIR = Path(__file__).parent


def main() -> None:
    missing = [
        name for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
        if not os.environ.get(name)
    ]
    if missing:
        print(f"{', '.join(missing)} not set -- orlog uses Anthropic, mem0 uses OpenAI.", file=sys.stderr)
        raise SystemExit(1)

    print("Running orlog...")
    run_orlog.main(BENCH_DIR / "orlog_results.json")
    print("Running mem0...")
    run_mem0.main(BENCH_DIR / "mem0_results.json")
    print("Scoring...")
    combined = score.main(
        BENCH_DIR / "orlog_results.json", BENCH_DIR / "mem0_results.json", BENCH_DIR / "results.json",
    )
    print(f"Wrote {BENCH_DIR / 'results.json'}")
    for system in ("orlog", "mem0"):
        m = combined[system]
        print(
            f"{system}: accuracy={m['accuracy']['overall']} "
            f"abstention={m['abstention']['correct_rate']} "
            f"query_p50_ms={m['query_known']['p50']}"
        )


if __name__ == "__main__":
    main()
