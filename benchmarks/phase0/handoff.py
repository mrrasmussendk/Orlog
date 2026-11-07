"""Does consulting orlog change the decision a later agent makes?

This is the narrow, deterministic core of "is the memory useful" -- NOT an
answer to it. What it measures is mechanical: a second agent picks up work
after the first one's context is gone, and acts on twelve facts about a
project. One arm acts on what a /compact-style summary would have carried
forward; the other calls recall(). Both are scored against what is
actually true now.

What this CANNOT tell you, and the reason it is a benchmark and not a
claim: there is no model here, no agent, and no judgment. Whether an agent
given better facts does better WORK is the Mimir plan's Phase 6 eval -- a
fixture repo, three real arms, planted rules, task success. This measures
only the step underneath that one: whether the facts it gets are right.
Necessary, not sufficient.

The baseline is built by orlog itself, on purpose. Arm A's knowledge is
`recall(key, known_as_of=<the compaction instant>)` -- literally what the
record knew when the summary was written. That makes the control
impossible to rig in orlog's favour: it comes out of the same code path as
the treatment, with one argument changed.

This harness reports correctness only, and deliberately reports no token
figure. Both arms here run through recall(), so the only thing separating
their payload sizes is the horizon string inside truth_version -- an
artifact of how the control is built, not a property of either approach.
What an answer costs in context is measured properly in context.py,
against re-reading the record.

The fixture deliberately contains cases where orlog does not help. Facts
that never changed cost a lookup and return what the summary already had.
A fact nobody ever recorded is not rescued -- orlog abstains, which is a
better failure than a confident wrong answer, but it is still a failure to
answer. Both are counted.

    python -m benchmarks.phase0.handoff --out handoff.json
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from orlog.config import OrlogConfig, RetrievalConfig, WorkspaceConfig
from orlog.runtime import Runtime
from orlog.server_tools import recall_tool, remember_tool
from orlog.vault import generate_key
from orlog.workspace import Workspace

#: Session 1 happens here; the summary is written at COMPACTED_AT; the
#: corrections land after it; agent 2 picks the work up at NOW.
SESSION_1 = datetime(2026, 3, 1, tzinfo=timezone.utc)
COMPACTED_AT = SESSION_1 + timedelta(hours=2)
CORRECTED_AT = SESSION_1 + timedelta(hours=3)
NOW = SESSION_1 + timedelta(hours=5)


@dataclass(frozen=True)
class Decision:
    """One thing the second agent has to know before it edits a file."""

    entity: str
    attribute: str
    original: str
    corrected: str | None  # None: nobody ever changed it
    recorded: bool = True  # False: nobody ever wrote it down


#: 12 decisions. 4 are corrected after the summary is written (the case
#: orlog exists for), 6 never change (the case where it adds nothing but
#: cost), and 2 were never recorded at all (the case it cannot rescue).
#:
#: No `original` may be a substring of its own `corrected` value, or
#: "did it serve the new value" and "is the old one gone" stop being
#: separable questions -- the same trap vs_mem0's dataset documents.
#: tests/test_handoff_usefulness.py enforces it.
FIXTURE: list[Decision] = [
    # -- corrected after the summary was written ------------------------
    Decision("src/orlog/server.py", "mcp_pin", "any 1.x or later", "capped below 2.0"),
    Decision("src/orlog/config.py", "toml_reader", "tomli backport", "stdlib tomllib"),
    Decision("pyproject.toml", "python_floor", "3.10", "3.11"),
    Decision("src/orlog/storage.py", "event_count", "recount every segment", "carry a running total"),
    # -- never changed: the summary was already right -------------------
    Decision("src/orlog/urd.py", "serialization", "JSONL, one event per line", None),
    Decision("src/orlog/urd.py", "hash_algorithm", "sha256 over canonical JSON", None),
    Decision("src/orlog/heimdall.py", "verifier_default", "windows backend", None),
    Decision("src/orlog/vault.py", "encryption", "AES-GCM", None),
    Decision("src/orlog/scrub.py", "detectors", "regex pack", None),
    Decision("tests/conformance", "runner", "pytest, five cells", None),
    # -- never recorded: orlog cannot rescue these ----------------------
    Decision("src/orlog/muninn.py", "eviction", "LRU, bounded", None, recorded=False),
    Decision("src/orlog/skuld.py", "decay_schedule", "nightly", None, recorded=False),
]


def truth(d: Decision) -> str:
    return d.corrected or d.original


def _runtime() -> Runtime:
    os.environ.setdefault("ORLOG_VAULT_KEY", generate_key())
    return Runtime(
        Workspace(tempfile.mkdtemp(prefix="orlog-handoff-")),
        OrlogConfig(
            workspace=WorkspaceConfig(name="handoff"),
            retrieval=RetrievalConfig(embedder="hashing"),
        ),
    )


def _seed(runtime: Runtime) -> None:
    for d in FIXTURE:
        if not d.recorded:
            continue
        remember_tool(
            runtime, f"{d.attribute} for {d.entity} is {d.original}",
            occurred_at=SESSION_1.isoformat(), recorded_at=SESSION_1.isoformat(),
            entity=d.entity, attribute=d.attribute, value=d.original,
            evidence_span=d.original,
        )
    for d in FIXTURE:
        if not d.recorded or d.corrected is None:
            continue
        remember_tool(
            runtime, f"{d.attribute} for {d.entity} is now {d.corrected}",
            occurred_at=CORRECTED_AT.isoformat(), recorded_at=CORRECTED_AT.isoformat(),
            entity=d.entity, attribute=d.attribute, value=d.corrected,
            evidence_span=d.corrected,
        )


def _score(runtime: Runtime, *, known_as_of: str | None) -> dict:
    """Run one arm. `known_as_of` pins the arm to what the record knew at
    that instant -- the compacted summary -- or None for "ask now"."""
    acted_correctly = acted_on_stale = abstained = 0
    rows = []
    for d in FIXTURE:
        answer = recall_tool(runtime, f"{d.entity}.{d.attribute}", known_as_of=known_as_of)
        claim = answer.get("claim") or ""
        if not answer.get("verified"):
            abstained += 1
            outcome = "abstained"
        elif truth(d) in claim:
            acted_correctly += 1
            outcome = "correct"
        else:
            acted_on_stale += 1
            outcome = "stale"
        rows.append({
            "entity": d.entity, "attribute": d.attribute, "outcome": outcome,
            "expected": truth(d), "served": claim or None,
        })
    return {
        "acted_correctly": acted_correctly,
        "acted_on_stale": acted_on_stale,
        "abstained": abstained,
        "decisions": len(FIXTURE),
        "rows": rows,
    }


def run() -> dict:
    runtime = _runtime()
    try:
        _seed(runtime)
        # Arm A: the second agent knows what the summary carried forward,
        # i.e. what the record knew when it was written.
        arm_a = _score(runtime, known_as_of=COMPACTED_AT.isoformat())
        # Arm B: the second agent asks.
        arm_b = _score(runtime, known_as_of=None)
    finally:
        runtime.close()

    planted = sum(1 for d in FIXTURE if d.corrected is not None)
    unrecorded = sum(1 for d in FIXTURE if not d.recorded)
    return {
        "fixture": {
            "decisions": len(FIXTURE),
            "corrected_after_the_summary": planted,
            "never_changed": len(FIXTURE) - planted - unrecorded,
            "never_recorded": unrecorded,
        },
        "arm_a_compacted_summary": {k: v for k, v in arm_a.items() if k != "rows"},
        "arm_b_asks_orlog": {k: v for k, v in arm_b.items() if k != "rows"},
        "delta": {
            "stale_decisions_avoided": arm_a["acted_on_stale"] - arm_b["acted_on_stale"],
            "decisions_orlog_could_not_rescue": arm_b["abstained"],
        },
        "rows": arm_b["rows"],
        "caveat": (
            "No model, no agent, no judgment. Measures whether the facts a "
            "second agent acts on are current -- not whether it does better "
            "work with them. That is the Mimir plan's Phase 6 eval."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.phase0.handoff")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    report = run()
    text = json.dumps(report, indent=2)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(text + "\n")

    a, b = report["arm_a_compacted_summary"], report["arm_b_asks_orlog"]
    f = report["fixture"]
    print(f"{f['decisions']} decisions: {f['corrected_after_the_summary']} corrected after the "
          f"summary, {f['never_changed']} never changed, {f['never_recorded']} never recorded\n")
    print(f"{'':<26}{'correct':>9}{'stale':>8}{'abstain':>9}")
    print(f"{'A  compacted summary':<26}{a['acted_correctly']:>9}{a['acted_on_stale']:>8}{a['abstained']:>9}")
    print(f"{'B  asks orlog':<26}{b['acted_correctly']:>9}{b['acted_on_stale']:>8}{b['abstained']:>9}")
    d = report["delta"]
    print(f"\nstale decisions avoided: {d['stale_decisions_avoided']}"
          f"   not rescued: {d['decisions_orlog_could_not_rescue']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
