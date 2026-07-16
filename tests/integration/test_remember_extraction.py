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

import pytest

from orlog.config import OrlogConfig, RetrievalConfig, WorkspaceConfig
from orlog.runtime import Runtime
from orlog.server import build_server
from orlog.server_tools import remember_tool
from orlog.vault import generate_key
from orlog.workspace import Workspace

anthropic = pytest.importorskip("anthropic")

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


def _ask_model_to_call_tools(mcp, user_message):
    """Sends `user_message` to a real Claude model with the exact tool
    schemas server.py already ships, and returns EVERY (name, input) tool
    call in its response, in order -- for scenarios where one message
    plausibly produces more than one tool call (e.g. two facts in one
    sentence). This is the point of these tests: proving the EXISTING
    descriptions are enough on their own, not a specially-crafted
    test-only prompt.
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
    return [(b.name, b.input) for b in tool_use_blocks]


def _ask_model_to_call_a_tool(mcp, user_message):
    """Returns just the first tool call the model made -- see
    _ask_model_to_call_tools for scenarios that need every call.
    """
    return _ask_model_to_call_tools(mcp, user_message)[0]


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


def test_two_same_named_entities_get_distinguishing_entity_detail(mcp):
    name1, args1 = _ask_model_to_call_a_tool(
        mcp, "Remember this: Anna, my coworker at Acme, moved to Seattle."
    )
    assert name1 == "remember"
    assert "anna" in args1["entity"].lower()
    assert "seattle" in args1["value"].lower()
    _execute_tool_call(mcp, name1, args1)

    name2, args2 = _ask_model_to_call_a_tool(
        mcp, "Remember this too: Anna, my college roommate, moved to Chicago."
    )
    assert name2 == "remember"
    assert "anna" in args2["entity"].lower()
    assert "chicago" in args2["value"].lower()
    # The two Annas must stay distinguishable -- either via entity_detail,
    # or some other entity string the model chose that keeps them apart.
    assert args1.get("entity_detail") != args2.get("entity_detail") or args1["entity"] != args2["entity"]
    _execute_tool_call(mcp, name2, args2)

    recall_name, recall_args = _ask_model_to_call_a_tool(mcp, "Where does my coworker Anna from Acme live now?")
    assert recall_name == "recall"
    answer = _execute_tool_call(mcp, recall_name, recall_args)
    assert answer["verified"] is True
    assert "seattle" in answer["claim"].lower()


def test_a_negated_dietary_fact_keeps_attribute_generic_and_queryable(mcp):
    # Same class of risk as the allergy scenario above, from a different
    # angle (negation, different domain): a phrasing like "doesn't eat X"
    # invites folding X into the attribute name (e.g. attribute="no_gluten",
    # value="true") just as readily as "is allergic to X" did originally.
    name, arguments = _ask_model_to_call_a_tool(mcp, "Marc doesn't eat gluten.")

    assert name == "remember"
    assert "marc" in arguments["entity"].lower()
    assert "gluten" in arguments["value"].lower()

    _execute_tool_call(mcp, name, arguments)
    recall_name, recall_arguments = _ask_model_to_call_a_tool(mcp, "What can't Marc eat?")
    assert recall_name == "recall"
    answer = _execute_tool_call(mcp, recall_name, recall_arguments)
    assert answer["verified"] is True
    assert "gluten" in answer["claim"].lower()


def test_a_preference_statement_becomes_a_well_formed_remember_call(mcp):
    name, arguments = _ask_model_to_call_a_tool(mcp, "Anna likes hiking.")

    assert name == "remember"
    assert "anna" in arguments["entity"].lower()
    assert "hiking" in arguments["value"].lower()

    _execute_tool_call(mcp, name, arguments)
    recall_name, recall_arguments = _ask_model_to_call_a_tool(mcp, "What does Anna like to do?")
    assert recall_name == "recall"
    answer = _execute_tool_call(mcp, recall_name, recall_arguments)
    assert answer["verified"] is True
    assert "hiking" in answer["claim"].lower()


def test_two_facts_in_one_sentence_produce_two_separate_remember_calls(mcp):
    # remember()'s schema holds exactly one attribute=value pair per call,
    # so two distinct facts about the same entity in one sentence should
    # become two separate remember() calls, not one call that drops a fact
    # or crams both into a single value.
    calls = _ask_model_to_call_tools(mcp, "Marc lives in Berlin and works as a software engineer.")
    remember_calls = [(name, args) for name, args in calls if name == "remember"]

    assert len(remember_calls) >= 2, f"expected 2 separate remember() calls, got: {calls!r}"
    assert all("marc" in args["entity"].lower() for _, args in remember_calls)
    values = " ".join(args["value"].lower() for _, args in remember_calls)
    assert "berlin" in values
    assert "engineer" in values


def test_a_correction_extracts_only_the_new_value_not_the_superseded_one(mcp, runtime):
    # Two prior attempts established: (1) establishing the fact only in
    # orlog's own store isn't enough -- the model never sees that store
    # directly, only the conversation; (2) even a self-contained update
    # directive isn't enough on its own -- the model wrote the update under
    # its OWN chosen entity casing/attribute name ("Anna"/"location")
    # instead of the one already on record ("anna"/"city"), fragmenting the
    # fact into two disconnected, tied-ambiguous keys. The realistic fix:
    # give the model an actual PRIOR TURN in the conversation where it
    # already looked the fact up (and so has genuinely seen "anna.city"),
    # then ask it to correct that same fact -- not just an isolated message
    # with no conversational history.
    remember_tool(runtime, "Anna lives in Seattle", entity="anna", attribute="city", value="Seattle")

    client = anthropic.Anthropic()
    tools = _anthropic_tools(mcp)
    messages = [{"role": "user", "content": "Where does Anna live?"}]

    first_response = client.messages.create(model=MODEL, max_tokens=500, system=SYSTEM_PROMPT, tools=tools, messages=messages)
    first_tool_use = [b for b in first_response.content if b.type == "tool_use"]
    assert first_tool_use, f"model did not call any tool for the initial question; response was: {first_response.content!r}"
    first_call = first_tool_use[0]
    assert first_call.name == "recall"
    first_answer = _execute_tool_call(mcp, first_call.name, first_call.input)
    assert first_answer["verified"] is True
    assert "seattle" in first_answer["claim"].lower()

    messages.append({"role": "assistant", "content": first_response.content})
    messages.append({
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": first_call.id, "content": json.dumps(first_answer)},
            {"type": "text", "text": "She just moved to Chicago, she's no longer in Seattle. Please update your records."},
        ],
    })

    second_response = client.messages.create(model=MODEL, max_tokens=500, system=SYSTEM_PROMPT, tools=tools, messages=messages)
    second_tool_use = [b for b in second_response.content if b.type == "tool_use"]
    assert second_tool_use, f"model did not call any tool for the correction; response was: {second_response.content!r}"
    second_call = second_tool_use[0]

    assert second_call.name == "remember"
    arguments = second_call.input
    assert "anna" in arguments["entity"].lower()
    assert "chicago" in arguments["value"].lower()
    assert "seattle" not in arguments["value"].lower()

    _execute_tool_call(mcp, second_call.name, arguments)
    recall_name, recall_arguments = _ask_model_to_call_a_tool(mcp, "Where does Anna live now?")
    assert recall_name == "recall"
    answer = _execute_tool_call(mcp, recall_name, recall_arguments)
    assert answer["verified"] is True
    assert "chicago" in answer["claim"].lower()


def test_a_numeric_fact_becomes_a_well_formed_remember_call(mcp):
    name, arguments = _ask_model_to_call_a_tool(mcp, "Marc is 34 years old.")

    assert name == "remember"
    assert "marc" in arguments["entity"].lower()
    assert "34" in arguments["value"]

    _execute_tool_call(mcp, name, arguments)
    recall_name, recall_arguments = _ask_model_to_call_a_tool(mcp, "How old is Marc?")
    assert recall_name == "recall"
    answer = _execute_tool_call(mcp, recall_name, recall_arguments)
    assert answer["verified"] is True
    assert "34" in answer["claim"]


def test_a_natural_language_question_becomes_a_well_formed_recall_call(mcp, runtime):
    # The reverse direction: the WRITE side is set up deterministically
    # (no live call needed for that part) so only the thing actually under
    # test -- does a natural-language question produce a correct recall()
    # call -- costs one real model call.
    remember_tool(
        runtime, "Anna moved to Copenhagen last spring", entity="anna", attribute="city", value="Copenhagen",
    )

    name, arguments = _ask_model_to_call_a_tool(mcp, "Do you know where Anna lives?")

    assert name == "recall"
    answer = _execute_tool_call(mcp, name, arguments)
    assert answer["verified"] is True
    assert "copenhagen" in answer["claim"].lower()
