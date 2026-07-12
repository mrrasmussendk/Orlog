"""Contract: Runtime wires a Workspace + Config into a working remember()
-> build_pipeline() -> answer() round trip.
"""

from datetime import datetime, timezone

import pytest

from orlog.config import OrlogConfig, WorkspaceConfig
from orlog.runtime import Runtime
from orlog.vault import generate_key
from orlog.workspace import Workspace

T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    monkeypatch.setenv("ORLOG_VAULT_KEY", generate_key())


@pytest.fixture
def runtime(tmp_path):
    ws = Workspace(tmp_path / "myproject")
    config = OrlogConfig(workspace=WorkspaceConfig(name="myproject"))
    rt = Runtime(ws, config)
    yield rt
    rt.close()


def test_remember_appends_a_scrubbed_fact_event(runtime):
    event = runtime.remember(
        "user email is alice@example.com", occurred_at=T1,
        entity="user:1", attribute="email", value="alice@example.com",
    )
    assert "alice@example.com" not in event.payload["text"]
    assert event.payload["entity"] == "user:1"


def test_build_pipeline_returns_none_with_no_fact_events(runtime):
    runtime.remember("just a note", occurred_at=T1)  # no entity/attribute/value
    assert runtime.build_pipeline(now=NOW) is None


def test_remember_then_recall_round_trips(runtime):
    runtime.remember("plan is pro", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    pipeline = runtime.build_pipeline(now=NOW)
    result = pipeline.answer("q1", "user:1", "plan", NOW, now=NOW)

    assert result.verified is True
    assert result.claim == "user:1.plan = pro"


def test_stats_are_shared_across_pipeline_calls(runtime):
    runtime.remember("plan is pro", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    pipeline = runtime.build_pipeline(now=NOW)
    pipeline.answer("q1", "user:1", "plan", NOW, now=NOW)

    assert runtime.stats.snapshot()["appends"] == 1
    assert runtime.stats.snapshot()["recalls"] == 1


def test_index_is_populated_on_remember(runtime):
    runtime.remember("plan is pro", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    rows = runtime.index._conn.execute("SELECT COUNT(*) FROM events_idx").fetchone()
    assert rows[0] == 1
