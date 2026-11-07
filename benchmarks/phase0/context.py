"""Context cost and verify-or-abstain correctness, measured offline.

Two questions the latency benchmark (bench.py) does not answer:

1. How much context does it cost an agent to get one answer, and how much
   of that context is about the thing it asked? Measured against the
   baseline a coding agent without a memory layer actually falls back to --
   re-reading the record itself.

2. When the answer is served, is it the right one, and when it is not
   known, does the system say so? Run over benchmarks/vs_mem0's dataset
   (40 facts, 10 of them later superseded, 15 never-stored questions) with
   the ScriptedDeriver and exact-key queries, so there are no API calls and
   no embedder in the loop -- what is measured is the deterministic
   verify-or-abstain pipeline, not a model's extraction skill or an
   embedding's recall.

Token counts are chars / 3.5, the estimator the Mimir plan uses for the
same purpose. They are estimates and are labelled as such everywhere.

    python -m benchmarks.phase0.context --out context.json
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

from orlog.config import OrlogConfig, RetrievalConfig, WorkspaceConfig
from orlog.runtime import Runtime
from orlog.server_tools import (
    list_attributes_tool,
    list_entities_tool,
    recall_history_tool,
    recall_tool,
    remember_tool,
)
from orlog.vault import generate_key
from orlog.workspace import Workspace

from benchmarks.vs_mem0.dataset import (
    INITIAL_FACTS,
    KNOWN_QUESTIONS,
    UNKNOWN_QUESTIONS,
    UPDATE_FACTS,
)

CHARS_PER_TOKEN = 3.5
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def est_tokens(text: str) -> int:
    return round(len(text) / CHARS_PER_TOKEN)


def _payload_tokens(obj) -> int:
    return est_tokens(json.dumps(obj, separators=(",", ":")))


def _runtime(name: str) -> Runtime:
    os.environ["ORLOG_VAULT_KEY"] = generate_key()
    return Runtime(
        Workspace(tempfile.mkdtemp(prefix=f"orlog-ctx-{name}-")),
        OrlogConfig(
            workspace=WorkspaceConfig(name=name),
            # scripted deriver + hashing embedder: no API calls, no model
            # download, and nothing probabilistic in the measurement.
            retrieval=RetrievalConfig(embedder="hashing"),
        ),
    )


# --------------------------------------------------------------- part 1
def correctness() -> dict:
    """40 facts, 10 superseded, 15 unknown. Exact-key queries."""
    runtime = _runtime("correctness")
    try:
        for fact in INITIAL_FACTS:
            remember_tool(
                runtime, fact.statement, occurred_at=T0.isoformat(),
                entity=fact.entity, attribute=fact.attribute, value=fact.value,
            )
        for fact in UPDATE_FACTS:
            remember_tool(
                runtime, fact.statement, occurred_at=(T0 + timedelta(days=30)).isoformat(),
                entity=fact.entity, attribute=fact.attribute, value=fact.value,
            )

        superseded = {(f.entity, f.attribute) for f in UPDATE_FACTS}
        rows = []
        for q in KNOWN_QUESTIONS:
            answer = recall_tool(runtime, f"{q.entity}.{q.attribute}")
            claim = answer.get("claim") or ""
            rows.append({
                "kind": "superseded" if (q.entity, q.attribute) in superseded else "stable",
                "verified": bool(answer.get("verified")),
                "abstained": bool(answer.get("abstained")),
                "correct": bool(answer.get("verified")) and q.expected_value in claim,
                "answer_tokens": _payload_tokens(answer),
            })
        for q in UNKNOWN_QUESTIONS:
            answer = recall_tool(runtime, f"{q.entity}.{q.attribute}")
            rows.append({
                "kind": "unknown",
                "verified": bool(answer.get("verified")),
                "abstained": bool(answer.get("abstained")),
                # The only outright failure for an unknown question is
                # confidently answering it.
                "correct": not answer.get("verified"),
                "answer_tokens": _payload_tokens(answer),
            })

        def tally(kind):
            group = [r for r in rows if r["kind"] == kind]
            return {
                "n": len(group),
                "correct": sum(r["correct"] for r in group),
                "verified": sum(r["verified"] for r in group),
                "abstained": sum(r["abstained"] for r in group),
                "accuracy_pct": round(100 * sum(r["correct"] for r in group) / len(group), 1),
                "mean_answer_tokens": round(
                    sum(r["answer_tokens"] for r in group) / len(group), 1
                ),
            }

        wrong = [r for r in rows if r["verified"] and not r["correct"]]
        return {
            "stable": tally("stable"),
            "superseded": tally("superseded"),
            "unknown": tally("unknown"),
            "confidently_wrong": len(wrong),
            "total_questions": len(rows),
        }
    finally:
        runtime.close()


# --------------------------------------------------------------- part 2
def context_cost(scales: list[int]) -> list[dict]:
    """What one answer costs in context, against reading the record."""
    out = []
    for n_facts in scales:
        runtime = _runtime(f"ctx{n_facts}")
        try:
            entities = max(1, n_facts // 8)
            for i in range(n_facts):
                entity, attribute = f"user:{i % entities}", f"attr_{(i // entities) % 8}"
                remember_tool(
                    runtime, f"{entity} {attribute} is v{i}",
                    occurred_at=(T0 + timedelta(seconds=i)).isoformat(),
                    entity=entity, attribute=attribute, value=f"v{i}",
                )

            key = "user:3.attr_0"
            answer = recall_tool(runtime, key)
            attrs = list_attributes_tool(runtime, "user:3")
            history = recall_history_tool(runtime, key)
            entity_list = list_entities_tool(runtime, limit=20)

            # The baseline: no memory layer, so the agent re-reads the
            # record itself to find one fact. This is what the Mimir plan's
            # arm A does when /compact drops something.
            raw_log = "".join(
                json.dumps(e.model_dump(mode="json"), separators=(",", ":")) + "\n"
                for e in runtime.log.read_all()
            )
            raw_tokens = est_tokens(raw_log)
            answer_tokens = _payload_tokens(answer)

            out.append({
                "facts": n_facts,
                "recall_tokens": answer_tokens,
                "list_attributes_tokens": _payload_tokens(attrs),
                "recall_history_tokens": _payload_tokens(history),
                "list_entities_20_tokens": _payload_tokens(entity_list),
                "raw_log_tokens": raw_tokens,
                # Of the context delivered, what share concerns the asked
                # fact? recall serves exactly one verified claim, so all of
                # it does; a raw re-read delivers one relevant fact among n.
                "recall_context_precision": 1.0,
                "raw_log_context_precision": round(answer_tokens / raw_tokens, 6),
                "tokens_saved_vs_raw": raw_tokens - answer_tokens,
                "reduction_x": round(raw_tokens / answer_tokens, 1),
            })
        finally:
            runtime.close()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.phase0.context")
    parser.add_argument("--scales", default="40,400,2000,10000")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    report = {
        "token_estimator": f"chars / {CHARS_PER_TOKEN}",
        "deriver": "scripted (no API calls)",
        "embedder": "hashing (offline); queries are exact-key, so retrieval is not in the loop",
        "correctness": correctness(),
        "context_cost": context_cost([int(s) for s in args.scales.split(",")]),
    }
    text = json.dumps(report, indent=2)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
