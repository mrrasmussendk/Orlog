"""run_mem0.py: drives a real mem0 Memory instance through the benchmark
dataset, timing every add()/search() call, and writes one JSON record per
item to mem0_results.json. All entities share a single Mem0 user_id
("vs_mem0_bench") -- mirroring orlog's single global event log, so both
systems face the same challenge of disambiguating the right entity from
free-text alone rather than relying on a pre-scoped partition per entity.
See docs/superpowers/specs/2026-07-17-vs-mem0-benchmark-design.md.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from mem0 import Memory

BENCHMARK_USER_ID = "vs_mem0_bench"


def build_memory(qdrant_path: Path, *, llm_model: str = "gpt-5-mini") -> Memory:
    return Memory.from_config({
        "vector_store": {"provider": "qdrant", "config": {"path": str(qdrant_path)}},
        "llm": {"provider": "openai", "config": {"model": llm_model}},
    })


def _ingest_record(entity: str, attribute: str, latency_ms: float, status: str, *, error: str | None = None) -> dict:
    return {
        "phase": "ingest", "entity": entity, "attribute": attribute, "latency_ms": latency_ms,
        "status": status, "error": error, "returned_text": None, "expected_value": None,
        "abstained": None, "tokens": None,
    }


def _query_record(
    phase: str, entity: str, attribute: str, latency_ms: float, status: str, *,
    expected_value: str | None = None, results: list[dict] | None = None, error: str | None = None,
) -> dict:
    top = results[0] if results else None
    returned_text = top.get("memory") if top else None
    abstained = (not bool(results)) if results is not None else None
    return {
        "phase": phase, "entity": entity, "attribute": attribute, "latency_ms": latency_ms,
        "status": status, "error": error, "returned_text": returned_text, "expected_value": expected_value,
        "abstained": abstained, "tokens": None,
    }


def ingest(memory: Memory, facts: list) -> list[dict]:
    records = []
    for fact in facts:
        started = time.perf_counter()
        try:
            memory.add(fact.statement, user_id=BENCHMARK_USER_ID)
            latency_ms = (time.perf_counter() - started) * 1000
            records.append(_ingest_record(fact.entity, fact.attribute, latency_ms, "ok"))
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            records.append(_ingest_record(fact.entity, fact.attribute, latency_ms, "error", error=str(exc)))
    return records


def run_queries(memory: Memory, questions: list, phase: str) -> list[dict]:
    records = []
    for q in questions:
        started = time.perf_counter()
        try:
            response = memory.search(q.question, filters={"user_id": BENCHMARK_USER_ID})
            latency_ms = (time.perf_counter() - started) * 1000
            records.append(_query_record(
                phase, q.entity, q.attribute, latency_ms, "ok",
                expected_value=q.expected_value, results=response.get("results", []),
            ))
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            records.append(_query_record(
                phase, q.entity, q.attribute, latency_ms, "error",
                expected_value=q.expected_value, error=str(exc),
            ))
    return records


def main(out_path: Path) -> list[dict]:
    from benchmarks.vs_mem0.dataset import INITIAL_FACTS, KNOWN_QUESTIONS, UNKNOWN_QUESTIONS, UPDATE_FACTS

    with tempfile.TemporaryDirectory(prefix="mem0_vs_orlog_") as tmp:
        memory = build_memory(Path(tmp) / "qdrant")
        try:
            records = []
            records += ingest(memory, INITIAL_FACTS + UPDATE_FACTS)
            records += run_queries(memory, KNOWN_QUESTIONS, "query_known")
            records += run_queries(memory, UNKNOWN_QUESTIONS, "query_unknown")
        finally:
            # Mem0's own Memory.close() only releases its SQLite *history* db
            # (~/.mem0/history.db, outside this temp dir) -- it does not touch
            # the vector store. Qdrant's local (path=) mode holds its own open
            # sqlite3 connections plus a portalocker .lock file under
            # qdrant_path, which sits inside `tmp`. Left open, those handles
            # make the TemporaryDirectory cleanup below raise PermissionError
            # on Windows (WinError 32) -- the same class of bug fixed for
            # orlog's Runtime.close() in run_orlog.py. Closing the Qdrant
            # client releases them before `tmp` is removed.
            memory.vector_store.client.close()
    Path(out_path).write_text(json.dumps(records, indent=2), encoding="utf-8")
    return records


if __name__ == "__main__":
    main(Path(__file__).parent / "mem0_results.json")
