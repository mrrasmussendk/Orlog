"""Render two bench.py reports (pre- and post-Phase 0) as a comparison.

    python -m benchmarks.phase0.compare before.json after.json --out results.json
"""
from __future__ import annotations

import argparse
import json


OPS = ["recall", "recall_history", "list_entities", "list_attributes", "check_action"]


def _speedup(before: float, after: float) -> str:
    if after <= 0:
        return "n/a"
    return f"{before / after:.1f}x"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.phase0.compare")
    parser.add_argument("before")
    parser.add_argument("after")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    before = json.loads(open(args.before, encoding="utf-8").read())
    after = json.loads(open(args.after, encoding="utf-8").read())
    assert not before["orlog_has_index_query_api"], f"{args.before} is a POST-Phase-0 report"
    assert after["orlog_has_index_query_api"], f"{args.after} is a PRE-Phase-0 report"

    rows = []
    for b, a in zip(before["scales"], after["scales"]):
        assert b["events"] == a["events"]
        row = {"events": b["events"], "ingest": {
            "before_events_per_s": b["ingest"]["events_per_s"],
            "after_events_per_s": a["ingest"]["events_per_s"],
            "speedup": _speedup(a["ingest"]["events_per_s"], b["ingest"]["events_per_s"]),
        }, "ops": {}}
        for op in OPS:
            row["ops"][op] = {
                "before_p50_ms": b[op]["p50_ms"],
                "after_p50_ms": a[op]["p50_ms"],
                "speedup": _speedup(b[op]["p50_ms"], a[op]["p50_ms"]),
            }
        rows.append(row)

    print(f"{'events':>7}  {'op':<16} {'before p50':>11} {'after p50':>10} {'speedup':>8}")
    for row in rows:
        n = row["events"]
        i = row["ingest"]
        print(f"{n:>7}  {'ingest (ev/s)':<16} {i['before_events_per_s']:>11} "
              f"{i['after_events_per_s']:>10} {i['speedup']:>8}")
        for op, v in row["ops"].items():
            print(f"{'':>7}  {op:<16} {v['before_p50_ms']:>9.3f}ms {v['after_p50_ms']:>8.3f}ms "
                  f"{v['speedup']:>8}")
        print()

    report = {"before": before["platform"], "python": after["python"], "rows": rows}
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
