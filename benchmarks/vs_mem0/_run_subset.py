"""Ad hoc parallel-worker helper: runs a specific subset of run indices
(orlog + mem0) into benchmarks/vs_mem0/runs/, skipping indices whose output
files already exist. Not part of the package's normal CLI -- spawned
directly, multiple times, with disjoint index ranges, to parallelize what
__main__.py normally does sequentially.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Mem0's Memory.__init__ also opens a *telemetry* vector store at a fixed,
# shared path outside any per-run tempdir (~/.mem0/migrations_qdrant) unless
# telemetry is disabled -- concurrent mem0 runs across worker processes
# otherwise collide on that store's file lock. Must be set before importing
# mem0 (mem0.memory.telemetry reads the env var at import time).
os.environ.setdefault("MEM0_TELEMETRY", "False")

from benchmarks.vs_mem0 import run_mem0, run_orlog  # noqa: E402

BENCH_DIR = Path(__file__).parent
RUNS_DIR = BENCH_DIR / "runs"


def main(indices: list[int]) -> None:
    RUNS_DIR.mkdir(exist_ok=True)
    for i in indices:
        orlog_path = RUNS_DIR / f"orlog_results_run{i}.json"
        mem0_path = RUNS_DIR / f"mem0_results_run{i}.json"
        if not orlog_path.exists():
            print(f"run{i}: orlog...", flush=True)
            run_orlog.main(orlog_path)
        if not mem0_path.exists():
            print(f"run{i}: mem0...", flush=True)
            run_mem0.main(mem0_path)
        print(f"run{i}: done", flush=True)


if __name__ == "__main__":
    indices = [int(x) for x in sys.argv[1].split(",")]
    main(indices)
