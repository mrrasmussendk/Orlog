"""score.py: combines run_orlog.py's and run_mem0.py's raw per-item JSON
result files into aggregated metrics for the benchmark dashboard.
"""

from __future__ import annotations

import json
from pathlib import Path

from benchmarks.vs_mem0 import grading


def load_raw_results(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def compute_latency_stats(records: list[dict], phase: str) -> dict:
    ok = [r for r in records if r["phase"] == phase and r["status"] == "ok"]
    errors = sum(1 for r in records if r["phase"] == phase and r["status"] == "error")
    stats = grading.percentiles([r["latency_ms"] for r in ok])
    stats["errors"] = errors
    return stats


def compute_accuracy(records: list[dict], updated_pairs: set[tuple[str, str]]) -> dict:
    known = [r for r in records if r["phase"] == "query_known" and r["status"] == "ok"]
    overall = [grading.is_correct(r["returned_text"], r["expected_value"]) for r in known]
    post_update = [
        grading.is_correct(r["returned_text"], r["expected_value"])
        for r in known
        if (r["entity"], r["attribute"]) in updated_pairs
    ]
    return {
        "overall": (sum(overall) / len(overall)) if overall else None,
        "overall_n": len(overall),
        "post_update": (sum(post_update) / len(post_update)) if post_update else None,
        "post_update_n": len(post_update),
    }


def compute_abstention(records: list[dict]) -> dict:
    unknown = [r for r in records if r["phase"] == "query_unknown" and r["status"] == "ok"]
    correct = [bool(r["abstained"]) for r in unknown]
    return {
        "correct_rate": (sum(correct) / len(correct)) if correct else None,
        "n": len(correct),
    }


def compute_tokens(records: list[dict]) -> dict:
    with_tokens = [r["tokens"] for r in records if r.get("tokens") is not None]
    return {"total": sum(with_tokens), "n_with_tokens": len(with_tokens)}


def compute_metrics(system_name: str, records: list[dict], updated_pairs: set[tuple[str, str]]) -> dict:
    return {
        "system": system_name,
        "ingest": compute_latency_stats(records, "ingest"),
        "query_known": compute_latency_stats(records, "query_known"),
        "query_unknown": compute_latency_stats(records, "query_unknown"),
        "accuracy": compute_accuracy(records, updated_pairs),
        "abstention": compute_abstention(records),
        "tokens": compute_tokens(records),
    }


def main(orlog_path: Path, mem0_path: Path, out_path: Path) -> dict:
    from benchmarks.vs_mem0.dataset import updated_pairs as get_updated_pairs

    pairs = get_updated_pairs()
    orlog_records = load_raw_results(orlog_path) if Path(orlog_path).exists() else []
    mem0_records = load_raw_results(mem0_path) if Path(mem0_path).exists() else []
    combined = {
        "orlog": compute_metrics("orlog", orlog_records, pairs),
        "mem0": compute_metrics("mem0", mem0_records, pairs),
    }
    Path(out_path).write_text(json.dumps(combined, indent=2), encoding="utf-8")
    return combined


if __name__ == "__main__":
    base = Path(__file__).parent
    main(base / "orlog_results.json", base / "mem0_results.json", base / "results.json")
