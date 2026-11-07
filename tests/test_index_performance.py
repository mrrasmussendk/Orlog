"""Contract: the scan tools stay fast at scale (ORLOG-SPEC.md §B12.4).

§B12.4's acceptance criterion is list_entities at p95 under 50 ms on a
100k-event workspace. Before the index query API that call read and parsed
every event in the workspace on every invocation, so the number it is
measuring is the whole point of the seam.

Marked `perf` and excluded from the default run (pyproject's addopts),
because building the fixture workspace costs tens of seconds:

    pytest -m perf tests/test_index_performance.py -s
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from orlog.config import OrlogConfig, RetrievalConfig, WorkspaceConfig
from orlog.models.event import EventDraft
from orlog.runtime import Runtime
from orlog.server_tools import list_attributes_tool, list_entities_tool, recall_history_tool
from orlog.storage import EventIndex, SegmentedLog
from orlog.vault import generate_key
from orlog.workspace import Workspace

pytestmark = pytest.mark.perf

EVENTS = 100_000
ENTITIES = 2_000
P95_BUDGET_MS = 50.0
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _percentile(samples: list[float], p: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(round(p * (len(ordered) - 1))))]


@pytest.fixture(scope="module")
def big_workspace(tmp_path_factory):
    """100k fact events, written straight through the log and indexed in
    one rebuild -- not through remember(), which would add scrub, schema
    checks and a commit per event to a fixture that is only here to be
    large."""
    root = tmp_path_factory.mktemp("big")
    ws = Workspace(root)
    ws.scaffold()
    log = SegmentedLog(ws.events_dir)

    started = time.perf_counter()
    for i in range(EVENTS):
        entity = f"user:{i % ENTITIES}"
        attribute = f"attr_{(i // ENTITIES) % 50}"
        log.append(
            EventDraft(
                occurred_at=T0 + timedelta(seconds=i),
                actor="bench",
                type="fact",
                payload={
                    "text": f"{entity} {attribute} is v{i}",
                    "entity": entity,
                    "attribute": attribute,
                    "value": f"v{i}",
                    "self_supported": True,
                },
            )
        )
    appended = time.perf_counter() - started

    index = EventIndex(ws.index_path)
    started = time.perf_counter()
    index.rebuild(log)
    rebuilt = time.perf_counter() - started
    index.close()

    print(f"\n{EVENTS} events appended in {appended:.1f}s, indexed in {rebuilt:.1f}s")
    return ws


@pytest.fixture(scope="module")
def runtime(big_workspace, tmp_path_factory):
    import os

    os.environ.setdefault("ORLOG_VAULT_KEY", generate_key())
    runtime = Runtime(
        big_workspace,
        OrlogConfig(
            workspace=WorkspaceConfig(name="big"), retrieval=RetrievalConfig(embedder="hashing")
        ),
    )
    yield runtime
    runtime.close()


def _measure(call, runs: int = 30) -> list[float]:
    call()  # warm the page cache and SQLite's own prepared-statement path
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1000)
    return samples


def test_list_entities_p95_under_50ms_at_100k_events(runtime):
    assert runtime.log.event_count() == EVENTS

    samples = _measure(lambda: list_entities_tool(runtime, limit=100))
    p95 = _percentile(samples, 0.95)
    print(f"list_entities p50={_percentile(samples, 0.5):.1f}ms p95={p95:.1f}ms")
    assert p95 < P95_BUDGET_MS


def test_list_entities_with_a_prefix_stays_under_budget(runtime):
    samples = _measure(lambda: list_entities_tool(runtime, prefix="user:1"))
    p95 = _percentile(samples, 0.95)
    print(f"list_entities(prefix) p95={p95:.1f}ms")
    assert p95 < P95_BUDGET_MS


def test_the_other_migrated_scans_are_under_budget_too(runtime):
    for label, call in [
        ("list_attributes", lambda: list_attributes_tool(runtime, "user:7")),
        ("recall_history", lambda: recall_history_tool(runtime, "user:7.attr_0")),
    ]:
        p95 = _percentile(_measure(call), 0.95)
        print(f"{label} p95={p95:.1f}ms")
        assert p95 < P95_BUDGET_MS, label
