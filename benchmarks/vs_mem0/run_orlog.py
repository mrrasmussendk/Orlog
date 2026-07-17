"""run_orlog.py: drives a real orlog Runtime through the benchmark dataset,
timing every write and query, and writes one JSON record per item to
orlog_results.json. Uses real Anthropic API calls via orlog's LLMDeriver
(deriver.backend="anthropic", claude-haiku-4-5 by default) for every query -- see
docs/superpowers/specs/2026-07-17-vs-mem0-benchmark-design.md.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from orlog.config import DeriverConfig, OrlogConfig, RetrievalConfig, WorkspaceConfig
from orlog.runtime import Runtime
from orlog.server_tools import recall_tool, remember_tool, stats_tool
from orlog.vault import generate_key
from orlog.workspace import Workspace

from benchmarks.vs_mem0.dataset import INITIAL_FACTS, KNOWN_QUESTIONS, UNKNOWN_QUESTIONS, UPDATE_FACTS


_API_KEY_ENV_BY_BACKEND = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def build_runtime(
    workspace_dir: Path, *, deriver_backend: str = "anthropic",
    deriver_model: str = "claude-haiku-4-5", embedder: str = "BAAI/bge-small-en-v1.5",
) -> Runtime:
    os.environ["ORLOG_VAULT_KEY"] = generate_key()
    config = OrlogConfig(
        workspace=WorkspaceConfig(name="vs_mem0_bench"),
        deriver=DeriverConfig(
            backend=deriver_backend, model=deriver_model,
            api_key_env=_API_KEY_ENV_BY_BACKEND.get(deriver_backend, "OPENAI_API_KEY"),
        ),
        retrieval=RetrievalConfig(embedder=embedder),
    )
    return Runtime(Workspace(workspace_dir), config)


def warm_up_embedder(runtime: Runtime) -> None:
    """Touches runtime.embedder once, outside any timed loop, so the first
    real query doesn't pay a cold-start model download.
    """
    _ = runtime.embedder


def _ingest_record(entity: str, attribute: str, latency_ms: float, status: str, *, error: str | None = None) -> dict:
    return {
        "phase": "ingest", "entity": entity, "attribute": attribute, "latency_ms": latency_ms,
        "status": status, "error": error, "returned_text": None, "expected_value": None,
        "abstained": None, "tokens": None,
    }


def _query_record(
    phase: str, entity: str, attribute: str, latency_ms: float, status: str, *,
    expected_value: str | None = None, answer: dict | None = None, tokens: dict | None = None,
    error: str | None = None,
) -> dict:
    returned_text = answer.get("claim") if answer else None
    abstained = answer.get("abstained") if answer else None
    total_tokens = (tokens["in"] + tokens["out"]) if tokens else None
    return {
        "phase": phase, "entity": entity, "attribute": attribute, "latency_ms": latency_ms,
        "status": status, "error": error, "returned_text": returned_text, "expected_value": expected_value,
        "abstained": abstained, "tokens": total_tokens,
    }


def ingest(runtime: Runtime, facts: list) -> list[dict]:
    records = []
    for fact in facts:
        started = time.perf_counter()
        try:
            remember_tool(runtime, fact.statement, entity=fact.entity, attribute=fact.attribute, value=fact.value)
            latency_ms = (time.perf_counter() - started) * 1000
            records.append(_ingest_record(fact.entity, fact.attribute, latency_ms, "ok"))
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            records.append(_ingest_record(fact.entity, fact.attribute, latency_ms, "error", error=str(exc)))
    return records


def run_queries(runtime: Runtime, questions: list, phase: str) -> list[dict]:
    records = []
    for q in questions:
        before = stats_tool(runtime)["tokens"]
        started = time.perf_counter()
        try:
            answer = recall_tool(runtime, q.question)
            latency_ms = (time.perf_counter() - started) * 1000
            after = stats_tool(runtime)["tokens"]
            diff = {"in": after["in"] - before["in"], "out": after["out"] - before["out"]}
            records.append(_query_record(
                phase, q.entity, q.attribute, latency_ms, "ok",
                expected_value=q.expected_value, answer=answer, tokens=diff,
            ))
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            records.append(_query_record(
                phase, q.entity, q.attribute, latency_ms, "error",
                expected_value=q.expected_value, error=str(exc),
            ))
    return records


def main(out_path: Path) -> list[dict]:
    with tempfile.TemporaryDirectory(prefix="orlog_vs_mem0_") as tmp:
        runtime = build_runtime(Path(tmp) / "workspace")
        try:
            warm_up_embedder(runtime)
            records = []
            records += ingest(runtime, INITIAL_FACTS + UPDATE_FACTS)
            records += run_queries(runtime, KNOWN_QUESTIONS, "query_known")
            records += run_queries(runtime, UNKNOWN_QUESTIONS, "query_unknown")
        finally:
            runtime.close()
    Path(out_path).write_text(json.dumps(records, indent=2), encoding="utf-8")
    return records


if __name__ == "__main__":
    main(Path(__file__).parent / "orlog_results.json")
