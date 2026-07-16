"""Integration tests: does a REAL model, given only server.py's existing
tool descriptions (no test-only prompt engineering), turn fuzzy natural
language into a well-formed remember()/recall() call?

Orlog itself never parses free text into entity/attribute/value -- that's
entirely the calling LLM's job (see docs/WHAT-IS-ORLOG.md: orlog's own two
AI-usage points are drafting an answer's wording and resolving a free-text
QUERY to a key on the read side, never extraction on the write side). So
this is a test of the calling LLM's tool-use behavior against server.py's
prose, not of any orlog code path -- the one place in this suite where
that's true.

Requires a live ANTHROPIC_API_KEY and makes real, billed API calls -- the
whole module is skipped otherwise so `pytest`/CI stays free and
deterministic by default. Run locally with the key exported:
    ANTHROPIC_API_KEY=sk-... ./.venv/Scripts/python.exe -m pytest tests/integration/ -v
"""

import asyncio
import json
import os

import anthropic
import pytest

from orlog.config import OrlogConfig, RetrievalConfig, WorkspaceConfig
from orlog.runtime import Runtime
from orlog.server import build_server
from orlog.server_tools import remember_tool
from orlog.vault import generate_key
from orlog.workspace import Workspace

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="requires a live ANTHROPIC_API_KEY -- these tests put a real model in the loop",
    ),
]

MODEL = os.environ.get("ORLOG_TEST_MODEL", "claude-haiku-4-5-20251001")

SYSTEM_PROMPT = (
    "You are an assistant with access to a persistent memory for this user. "
    "When the user tells you something worth remembering, call the remember "
    "tool. When they ask about something you might already know, call recall."
)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("ORLOG_VAULT_KEY", generate_key())
    config = OrlogConfig(
        workspace=WorkspaceConfig(name="integrationproject"),
        retrieval=RetrievalConfig(embedder="hashing", min_confidence=0.15, ambiguity_margin=0.85),
    )
    rt = Runtime(Workspace(tmp_path / "integrationproject"), config)
    yield rt
    rt.close()


@pytest.fixture
def mcp(runtime):
    return build_server(runtime)


def _anthropic_tools(mcp):
    tools = asyncio.run(mcp.list_tools())
    return [
        {"name": t.name, "description": t.description, "input_schema": t.inputSchema}
        for t in tools
    ]


def _ask_model_to_call_a_tool(mcp, user_message):
    """Sends `user_message` to a real Claude model with the exact tool
    schemas server.py already ships, and returns the (name, input) of the
    first tool_use block in its response. This is the point of these
    tests: proving the EXISTING descriptions are enough on their own, not a
    specially-crafted test-only prompt.
    """
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=500,
        system=SYSTEM_PROMPT,
        tools=_anthropic_tools(mcp),
        messages=[{"role": "user", "content": user_message}],
    )
    tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
    assert tool_use_blocks, f"model did not call any tool; response was: {response.content!r}"
    block = tool_use_blocks[0]
    return block.name, block.input


def _execute_tool_call(mcp, name, arguments):
    result = asyncio.run(mcp.call_tool(name, arguments))
    return json.loads(result[0].text)


def test_a_fuzzy_location_statement_becomes_a_well_formed_remember_call(mcp):
    name, arguments = _ask_model_to_call_a_tool(mcp, "Remember this: Anna lived in Copenhagen.")

    assert name == "remember"
    assert "anna" in arguments["entity"].lower()
    assert "copenhagen" in arguments["value"].lower()

    _execute_tool_call(mcp, name, arguments)
    recall_name, recall_arguments = _ask_model_to_call_a_tool(mcp, "Where does Anna live?")
    assert recall_name == "recall"
    answer = _execute_tool_call(mcp, recall_name, recall_arguments)
    assert answer["verified"] is True
    assert "copenhagen" in answer["claim"].lower()


def test_a_fuzzy_plan_statement_becomes_a_well_formed_remember_call(mcp):
    name, arguments = _ask_model_to_call_a_tool(mcp, "Remember that Marc's subscription plan is Pro.")

    assert name == "remember"
    assert "marc" in arguments["entity"].lower()
    assert "pro" in arguments["value"].lower()

    _execute_tool_call(mcp, name, arguments)
    recall_name, recall_arguments = _ask_model_to_call_a_tool(mcp, "What plan does Marc have?")
    assert recall_name == "recall"
    answer = _execute_tool_call(mcp, recall_name, recall_arguments)
    assert answer["verified"] is True
    assert "pro" in answer["claim"].lower()


def test_a_fuzzy_allergy_statement_becomes_a_well_formed_remember_call(mcp):
    name, arguments = _ask_model_to_call_a_tool(mcp, "Just so you know, Emma is allergic to shellfish.")

    assert name == "remember"
    assert "emma" in arguments["entity"].lower()
    assert "shellfish" in arguments["value"].lower()

    _execute_tool_call(mcp, name, arguments)
    recall_name, recall_arguments = _ask_model_to_call_a_tool(mcp, "What is Emma allergic to?")
    assert recall_name == "recall"
    answer = _execute_tool_call(mcp, recall_name, recall_arguments)
    assert answer["verified"] is True
    assert "shellfish" in answer["claim"].lower()
