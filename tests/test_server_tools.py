"""Contract: each MCP tool function (ORLOG-SPEC.md §B9) works against a
real Runtime, with no `mcp` SDK involved -- server.py's FastMCP wiring is
a thin, separately-untested layer on top of these.
"""

from datetime import datetime, timezone

import pytest

from orlog.config import OrlogConfig, WorkspaceConfig
from orlog.runtime import Runtime
from orlog.server_tools import check_action_tool, recall_history_tool, recall_tool, remember_tool, stats_tool
from orlog.vault import generate_key
from orlog.workspace import Workspace

T1 = "2026-01-01T00:00:00+00:00"
T2 = "2026-06-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    monkeypatch.setenv("ORLOG_VAULT_KEY", generate_key())


@pytest.fixture
def runtime(tmp_path):
    rt = Runtime(Workspace(tmp_path / "myproject"), OrlogConfig(workspace=WorkspaceConfig(name="myproject")))
    yield rt
    rt.close()


def test_remember_tool_returns_an_event_id_and_scrubs_pii(runtime):
    result = remember_tool(runtime, "email is alice@example.com", occurred_at=T1, entity="user:1", attribute="email", value="alice@example.com")

    assert "event_id" in result
    event = runtime.log.get(result["event_id"])
    assert "alice@example.com" not in event.payload["text"]


def test_recall_tool_returns_a_verified_answer(runtime):
    remember_tool(runtime, "plan is pro", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    result = recall_tool(runtime, "user:1.plan", as_of=T2)

    assert result["verified"] is True
    assert result["claim"] == "user:1.plan = pro"


def test_recall_tool_abstains_with_no_facts_at_all(runtime):
    result = recall_tool(runtime, "user:1.plan", as_of=T2)
    assert result["abstained"] is True
    assert result["reasons"] == ["NO_CANDIDATES"]


def test_recall_tool_abstains_on_a_malformed_query(runtime):
    remember_tool(runtime, "plan is pro", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    result = recall_tool(runtime, "not-a-dotted-query", as_of=T2)

    assert result["abstained"] is True
    assert result["reasons"] == ["NO_CANDIDATES"]


def test_recall_history_tool_returns_the_full_chain(runtime):
    remember_tool(runtime, "plan is free", occurred_at=T1, entity="user:1", attribute="plan", value="free")
    remember_tool(runtime, "plan is pro", occurred_at=T2, entity="user:1", attribute="plan", value="pro")

    result = recall_history_tool(runtime, "user:1.plan")

    assert [entry["value"] for entry in result["chain"]] == ["free", "pro"]
    assert result["chain"][0]["valid_to"] == T2
    assert result["chain"][1]["valid_to"] is None  # still open-ended


def test_check_action_tool_warns_after_enough_prior_failures(runtime):
    # Some fact data must exist so build_pipeline() succeeds and
    # pipeline.answer() itself runs (and records the abstention via skuld)
    # -- querying a DIFFERENT, nonexistent entity still abstains with
    # NO_CANDIDATES, but this time through the real ledger-recording path.
    remember_tool(runtime, "plan is pro", occurred_at=T1, entity="user:1", attribute="plan", value="pro")
    for _ in range(3):
        recall_tool(runtime, "user:2.plan", as_of=T2)  # abstains each time, query_id defaults to the query itself

    result = check_action_tool(runtime, "user:2.plan")

    assert result["prior_outcomes"]["failures"] == 3
    assert result["warnings"]


def test_stats_tool_reflects_runtime_activity(runtime):
    remember_tool(runtime, "plan is pro", occurred_at=T1, entity="user:1", attribute="plan", value="pro")
    recall_tool(runtime, "user:1.plan", as_of=T2)

    snapshot = stats_tool(runtime)

    assert snapshot["appends"] == 1
    assert snapshot["recalls"] == 1
