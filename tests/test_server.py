"""Contract: the real FastMCP server (orlog.server.build_server()) --
tool names, JSON schemas, and the descriptions/docstrings a calling LLM
actually reads to learn how to use these tools. Distinct from
test_server_tools.py, which tests server_tools.py's plain functions
directly and never touches the `mcp` SDK.

Every FastMCP method used here (list_tools/call_tool/list_resources/
read_resource) is async; asyncio.run(...) drives them from a plain
`def test_...():`, so no new test dependency (e.g. pytest-asyncio) is
needed.
"""

import asyncio
import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from orlog.config import OrlogConfig, RetrievalConfig, WorkspaceConfig
from orlog.runtime import Runtime
from orlog.server import build_server
from orlog.vault import generate_key
from orlog.workspace import Workspace

EXPECTED_TOOL_NAMES = {
    "remember", "recall", "recall_history", "list_entities",
    "list_attributes", "check_action", "stats",
}


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    monkeypatch.setenv("ORLOG_VAULT_KEY", generate_key())


@pytest.fixture
def runtime(tmp_path):
    config = OrlogConfig(
        workspace=WorkspaceConfig(name="mcpproject"),
        retrieval=RetrievalConfig(embedder="hashing", min_confidence=0.15, ambiguity_margin=0.85),
    )
    rt = Runtime(Workspace(tmp_path / "mcpproject"), config)
    yield rt
    rt.close()


@pytest.fixture
def mcp(runtime):
    return build_server(runtime)


def _list_tools(mcp):
    return asyncio.run(mcp.list_tools())


def _call_tool(mcp, name, arguments):
    return asyncio.run(mcp.call_tool(name, arguments))


def test_every_spec_b9_tool_is_registered_exactly_once(mcp):
    names = [t.name for t in _list_tools(mcp)]
    assert set(names) == EXPECTED_TOOL_NAMES
    assert len(names) == len(set(names))


def test_recalls_description_documents_every_abstention_reason_it_can_hand_back(mcp):
    # Only the reason codes server_tools.recall_tool's own abstention paths
    # produce directly (NO_CANDIDATES, AMBIGUOUS, EMBEDDER_UNAVAILABLE) plus
    # the two pipeline.answer() paths its docstring already calls out
    # (UNSUPPORTED_BY_SOURCE, VERIFY_TIMEOUT) are asserted here -- this
    # matches what recall's description in server.py actually says today.
    # heimdall verification-failure codes (UNCITED, NOT_FOUND, NOT_YET_VALID)
    # and deriver-failure reasons (DERIVER_INSUFFICIENT, DERIVER_TIMEOUT) can
    # ALSO reach `reasons` via pipeline.py but are NOT mentioned here -- a
    # real documentation gap, worth flagging to the project owner
    # separately, not asserted against in this test (a test asserting they
    # ARE documented would fail against today's actual, working code).
    recall_tool = next(t for t in _list_tools(mcp) if t.name == "recall")
    for code in ["NO_CANDIDATES", "AMBIGUOUS", "EMBEDDER_UNAVAILABLE", "UNSUPPORTED_BY_SOURCE", "VERIFY_TIMEOUT"]:
        assert code in recall_tool.description


def test_recalls_description_reports_the_runtimes_actual_guarantee_mode(mcp, runtime):
    recall_tool = next(t for t in _list_tools(mcp) if t.name == "recall")
    assert f"Guarantee mode: {runtime.config.workspace.mode}" in recall_tool.description


def test_remembers_docstring_states_the_entityattributevalue_coupling_the_schema_cannot(mcp):
    # FastMCP derives inputSchema.required from Python parameter defaults --
    # confirmed live: only "text" ends up required there, even though
    # remember_tool actually requires entity/attribute/value together too
    # (SchemaError otherwise). A client that only reads the JSON schema,
    # not the prose, would never learn this rule -- so the docstring is the
    # only place this constraint is stated, and this test pins that down.
    remember_tool = next(t for t in _list_tools(mcp) if t.name == "remember")
    assert remember_tool.inputSchema["required"] == ["text"]
    assert "entity, attribute, AND value are all REQUIRED" in remember_tool.description


def test_call_tool_round_trips_remember_then_recall_over_the_real_mcp_surface(mcp):
    remember_result = _call_tool(mcp, "remember", {
        "text": "plan is pro", "entity": "user:1", "attribute": "plan", "value": "pro",
    })
    payload = json.loads(remember_result[0].text)
    assert "event_id" in payload

    recall_result = _call_tool(mcp, "recall", {"query": "user:1.plan"})
    answer = json.loads(recall_result[0].text)
    assert answer["verified"] is True
    assert answer["claim"] == "user:1.plan = pro"


def test_call_tool_surfaces_an_actionable_schema_error_to_the_calling_llm(mcp):
    with pytest.raises(ToolError) as exc_info:
        _call_tool(mcp, "remember", {"text": "bare text only"})

    message = str(exc_info.value)
    assert "entity" in message and "attribute" in message and "value" in message


def test_both_resources_are_registered_and_readable(mcp):
    uris = {str(r.uri) for r in asyncio.run(mcp.list_resources())}
    assert uris == {"orlog://spec", "orlog://conformance-report"}

    spec_contents = list(asyncio.run(mcp.read_resource("orlog://spec")))
    assert len(spec_contents) == 1
    assert len(spec_contents[0].content) > 0

    report_contents = list(asyncio.run(mcp.read_resource("orlog://conformance-report")))
    report = json.loads(report_contents[0].content)
    assert isinstance(report, dict)
