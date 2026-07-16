# Expanded Test Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close real, previously-untested gaps in orlog's remember/recall behavior, its MCP tool-registration surface, and its LLM adapter layer, and add live-model integration tests proving fuzzy natural language reliably becomes a well-formed `remember`/`recall` call.

**Architecture:** No new components. Every task adds test functions to an existing or new `tests/*.py` file, driving already-shipped code (`Runtime`, `server_tools.py`, `build_server()`, `LLMDeriver`, the Anthropic/OpenAI adapters) through its real public API. One task (Task 1) makes a single one-line production fix, found and explicitly approved during planning; every other task is additive, test-only.

**Tech Stack:** Python 3.10+, pytest, the `mcp` SDK's `FastMCP` (driven synchronously via `asyncio.run(...)`, no new test dependency), `anthropic`/`openai` SDKs (mocked in Tasks 2-3 via `monkeypatch`; called live, for real, only in Tasks 10-11).

## Global Constraints

- **Zero changes to `src/orlog/`** except Task 1's one-line guard fix in `runtime.py` (explicitly approved; every other task is test-only). If executing any other task surfaces what looks like a real behavior bug, STOP and report it — do not fix it silently as part of this plan.
- **No new dependencies.** `FastMCP.list_tools()`/`call_tool()`/`list_resources()`/`read_resource()` are async but are driven from plain `def test_...():` functions via `asyncio.run(...)` — confirmed working, no `pytest-asyncio` needed. SDK adapter tests use `monkeypatch` + `unittest`-style fake objects, not a mocking library.
- **House style**: one narratively-named test function per scenario with a short docstring/comment explaining *why* when it's non-obvious, matching every existing file in `tests/`. Use `@pytest.mark.parametrize` only for genuinely symmetric enumeration (Task 1's 3-way empty-string case).
- **Vault key fixture**: every test module that builds a `Runtime` needs `monkeypatch.setenv("ORLOG_VAULT_KEY", generate_key())`, via an autouse fixture — copy the exact pattern already in `tests/test_server_tools.py`.
- **Test runner**: a project venv already exists at `.venv` (Python 3.14, installed via `pip install -e ".[dev,anthropic,openai]"`). Run tests with `./.venv/Scripts/python.exe -m pytest <path> -v`. Baseline: 211 tests pass before this plan starts.
- **Part A tasks (1-9)** must pass with no network access and no API key. **Part B tasks (10-11)** require a live `ANTHROPIC_API_KEY` and must skip cleanly (not fail, not error) when it's absent.
- Every piece of test code in this plan has already been run successfully against the current codebase during planning (except Tasks 10-11, which need a real API key this planning environment doesn't have) — implementers should not need to debug the *approach*, only wire it up file-by-file.

---

### Task 1: Close the empty-string `remember()` gap + partial-write contract coverage

**Files:**
- Modify: `src/orlog/runtime.py:195`
- Test: `tests/test_remember_contract.py`

**Interfaces:**
- Consumes: `orlog.server_tools.remember_tool`/`recall_tool`, `orlog.errors.SchemaError` (all pre-existing).
- Produces: nothing new for later tasks — this task is self-contained.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_remember_contract.py` (after the existing `test_3_...` function):

```python
@pytest.mark.parametrize("missing", ["entity", "attribute", "value"])
def test_4_empty_string_entity_attribute_or_value_is_rejected_same_as_none(runtime, missing):
    # Found during test-coverage work (2026-07-16): Runtime.remember()'s
    # guard only checked `is None`, so an empty string silently bypassed it
    # and produced the exact same "dead-end memory" this file exists to
    # prevent -- a durable write, a success code, and a key that resolves to
    # nothing on recall. Fixed alongside this test (see runtime.py).
    fields = {"entity": "person:empty", "attribute": "office_preference", "value": "remote"}
    fields[missing] = ""

    with pytest.raises(SchemaError):
        remember_tool(runtime, text="Some text.", **fields)


def test_5_partial_write_entity_and_value_with_no_attribute_is_rejected(runtime):
    with pytest.raises(SchemaError) as exc_info:
        remember_tool(runtime, entity="person:mikkel", value="dislikes open-plan", text="Mikkel dislikes open-plan offices.")
    assert "entity" in str(exc_info.value)
    assert "attribute" in str(exc_info.value)
    assert "value" in str(exc_info.value)
    answer = recall_tool(runtime, "person:mikkel.office_preference")
    assert answer["abstained"] is True


def test_6_partial_write_attribute_and_value_with_no_entity_is_rejected(runtime):
    with pytest.raises(SchemaError) as exc_info:
        remember_tool(runtime, attribute="office_preference", value="dislikes open-plan", text="Mikkel dislikes open-plan offices.")
    assert "entity" in str(exc_info.value)
    assert "attribute" in str(exc_info.value)
    assert "value" in str(exc_info.value)
```

No new imports needed — `pytest`, `SchemaError`, `remember_tool`, `recall_tool` are already imported at the top of this file.

- [ ] **Step 2: Run to verify the empty-string cases fail, the partial-write cases pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_remember_contract.py -v`

Expected: the 3 `test_4_...[entity|attribute|value]` cases FAIL with "DID NOT RAISE SchemaError"; `test_5_...` and `test_6_...` PASS (they already exercise the pre-existing `is None` guard, just with a different missing field than the original `test_2_...`).

- [ ] **Step 3: Apply the one-line fix**

In `src/orlog/runtime.py`, in `Runtime.remember()`:

```python
        if entity is None or attribute is None or value is None:
```

becomes:

```python
        if not entity or not attribute or not value:
```

(Same file, same line — `entity`/`attribute`/`value` are always `str | None`, so `not x` is `True` exactly when `x` is `None` or `""`; no other falsy string value is possible for these parameters.)

- [ ] **Step 4: Run to verify everything passes, and the full suite has no regressions**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_remember_contract.py -v`
Expected: all 6 tests PASS.

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: all tests pass (211 pre-existing + the new ones added so far).

- [ ] **Step 5: Commit**

```bash
git add src/orlog/runtime.py tests/test_remember_contract.py
git commit -m "fix: reject empty-string entity/attribute/value in remember(), same as None"
```

---

### Task 2: `tests/test_server.py` — the real MCP server surface

**Files:**
- Create: `tests/test_server.py`

**Interfaces:**
- Consumes: `orlog.server.build_server(runtime) -> FastMCP`, `orlog.config.OrlogConfig/RetrievalConfig/WorkspaceConfig`, `orlog.runtime.Runtime`, `orlog.workspace.Workspace`, `orlog.vault.generate_key` (all pre-existing).
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the test file**

Create `tests/test_server.py`:

```python
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
```

- [ ] **Step 2: Run to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_server.py -v`
Expected: all 7 tests PASS (this exercises already-correct behavior, so there is no red step here — it's new coverage, not a new feature).

- [ ] **Step 3: Sanity-check the tests are real (not vacuously true)**

Temporarily break one assertion's target to confirm the test suite catches it: in `src/orlog/server.py`, change the `remember` tool's docstring first line from `"""Append a scrubbed memory to the log. Returns {event_id}.` to `"""Append a memory.` (deleting the "entity, attribute, AND value are all REQUIRED" sentence isn't necessary — just confirm the test fails if that sentence is genuinely removed). Concretely: temporarily delete the paragraph containing "entity, attribute, AND value are all REQUIRED" from the `remember` docstring, run `./.venv/Scripts/python.exe -m pytest tests/test_server.py::test_remembers_docstring_states_the_entityattributevalue_coupling_the_schema_cannot -v`, confirm it now FAILS, then revert the deletion (`git checkout -- src/orlog/server.py`) and re-run to confirm it PASSES again.

- [ ] **Step 4: Commit**

```bash
git add tests/test_server.py
git commit -m "test: add coverage for the real MCP server surface (server.py)"
```

---

### Task 3: `tests/test_huginn_llm_adapters.py` — the Anthropic/OpenAI completion adapters

**Files:**
- Create: `tests/test_huginn_llm_adapters.py`

**Interfaces:**
- Consumes: `orlog.huginn_llm.AnthropicCompletion`, `orlog.huginn_llm.OpenAICompletion` (pre-existing).
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the test file**

Create `tests/test_huginn_llm_adapters.py`:

```python
"""Contract: AnthropicCompletion/OpenAICompletion -- the adapters that
actually build the request sent to a real model. Distinct from
test_huginn_llm.py, which tests LLMDeriver's prompt-contract logic against
a fake CompletionFn and never touches either SDK. Every test here
monkeypatches the SDK's client class itself, so nothing here ever makes a
network call or needs a real API key.
"""

import httpx
import pytest

from orlog.huginn_llm import AnthropicCompletion, OpenAICompletion


# -- Anthropic --


class _FakeAnthropicBlock:
    def __init__(self, type_, text=None):
        self.type = type_
        self.text = text


class _FakeAnthropicUsage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeAnthropicResponse:
    def __init__(self, content, usage):
        self.content = content
        self.usage = usage


class _FakeAnthropicMessages:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


class _FakeAnthropicClient:
    def __init__(self, *, response=None, error=None):
        self.messages = _FakeAnthropicMessages(response=response, error=error)


def _install_fake_anthropic_client(monkeypatch, fake_client):
    monkeypatch.setattr("anthropic.Anthropic", lambda *a, **k: fake_client)


def test_anthropic_completion_requires_the_api_key_env_var(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(RuntimeError):
        AnthropicCompletion(model="test-model")


def test_anthropic_completion_joins_text_across_multiple_blocks_and_skips_non_text(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    response = _FakeAnthropicResponse(
        content=[
            _FakeAnthropicBlock("text", "hello "),
            _FakeAnthropicBlock("tool_use"),  # no .text -- must never be concatenated
            _FakeAnthropicBlock("text", "world"),
        ],
        usage=_FakeAnthropicUsage(10, 5),
    )
    fake_client = _FakeAnthropicClient(response=response)
    _install_fake_anthropic_client(monkeypatch, fake_client)

    completion = AnthropicCompletion(model="test-model")
    text, usage = completion("system prompt", "user prompt", max_tokens=100)

    assert text == "hello world"
    assert usage == {"in": 10, "out": 5}


def test_anthropic_completion_passes_temperature_zero_and_the_configured_model(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    response = _FakeAnthropicResponse(content=[_FakeAnthropicBlock("text", "x")], usage=_FakeAnthropicUsage(1, 1))
    fake_client = _FakeAnthropicClient(response=response)
    _install_fake_anthropic_client(monkeypatch, fake_client)

    completion = AnthropicCompletion(model="claude-test")
    completion("sys", "user", max_tokens=42)

    call = fake_client.messages.calls[0]
    assert call["model"] == "claude-test"
    assert call["temperature"] == 0
    assert call["max_tokens"] == 42
    assert call["system"] == "sys"
    assert call["messages"] == [{"role": "user", "content": "user"}]


def test_anthropic_completion_converts_the_providers_timeout_into_a_plain_timeouterror(monkeypatch):
    import anthropic

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    request = httpx.Request("POST", "https://example.com")
    fake_client = _FakeAnthropicClient(error=anthropic.APITimeoutError(request=request))
    _install_fake_anthropic_client(monkeypatch, fake_client)

    completion = AnthropicCompletion(model="test-model")
    with pytest.raises(TimeoutError):
        completion("sys", "user", max_tokens=10)


# -- OpenAI --


class _FakeOpenAIMessage:
    def __init__(self, content):
        self.content = content


class _FakeOpenAIChoice:
    def __init__(self, content):
        self.message = _FakeOpenAIMessage(content)


class _FakeOpenAIUsage:
    def __init__(self, prompt_tokens, completion_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeOpenAIResponse:
    def __init__(self, content, usage):
        self.choices = [_FakeOpenAIChoice(content)]
        self.usage = usage


class _FakeOpenAICompletions:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


class _FakeOpenAIChat:
    def __init__(self, response=None, error=None):
        self.completions = _FakeOpenAICompletions(response=response, error=error)


class _FakeOpenAIClient:
    def __init__(self, *, response=None, error=None):
        self.chat = _FakeOpenAIChat(response=response, error=error)


def _install_fake_openai_client(monkeypatch, fake_client):
    monkeypatch.setattr("openai.OpenAI", lambda *a, **k: fake_client)


def test_openai_completion_requires_the_api_key_env_var(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(RuntimeError):
        OpenAICompletion(model="test-model")


def test_openai_completion_reads_text_and_usage_from_the_chat_completion_shape(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    response = _FakeOpenAIResponse(content="hi there", usage=_FakeOpenAIUsage(7, 3))
    fake_client = _FakeOpenAIClient(response=response)
    _install_fake_openai_client(monkeypatch, fake_client)

    completion = OpenAICompletion(model="test-model")
    text, usage = completion("sys", "user", max_tokens=50)

    assert text == "hi there"
    assert usage == {"in": 7, "out": 3}


def test_openai_completion_converts_the_providers_timeout_into_a_plain_timeouterror(monkeypatch):
    import openai

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    request = httpx.Request("POST", "https://example.com")
    fake_client = _FakeOpenAIClient(error=openai.APITimeoutError(request=request))
    _install_fake_openai_client(monkeypatch, fake_client)

    completion = OpenAICompletion(model="test-model")
    with pytest.raises(TimeoutError):
        completion("sys", "user", max_tokens=10)
```

- [ ] **Step 2: Run to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_huginn_llm_adapters.py -v`
Expected: all 6 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_huginn_llm_adapters.py
git commit -m "test: add coverage for AnthropicCompletion/OpenAICompletion adapters"
```

---

### Task 4: `tests/test_server_tools.py` — more `remember`/`recall` edge cases

**Files:**
- Modify: `tests/test_server_tools.py`

**Interfaces:**
- Consumes: `orlog.server_tools.remember_tool`/`recall_tool`, `orlog.config.SchemaConfig`, `orlog.retrieval_hybrid.KeyCandidate` (all pre-existing).
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the tests**

Add to `tests/test_server_tools.py` (add `from orlog.config import SchemaConfig` and `from orlog.retrieval_hybrid import KeyCandidate` to the existing import block at the top, alongside the other `orlog.config` imports already there):

```python
def test_entity_detail_on_a_first_not_yet_disambiguated_write_is_accepted(runtime):
    result = remember_tool(
        runtime, "Anna the coworker moved to Seattle", occurred_at=T1,
        entity="anna", attribute="city", value="Seattle", entity_detail="coworker at Acme",
    )
    assert "event_id" in result
    answer = recall_tool(runtime, "anna#coworker at Acme.city", as_of=T2)
    assert answer["verified"] is True
    assert answer["claim"] == "anna#coworker at Acme.city = Seattle"


def test_register_new_type_and_register_new_attribute_together_for_a_brand_new_type(tmp_path):
    config = OrlogConfig(
        workspace=WorkspaceConfig(name="schemaproject2"),
        retrieval=RetrievalConfig(embedder="hashing"),
        schema_=SchemaConfig(known_types=["user"]),
    )
    rt = Runtime(Workspace(tmp_path / "schemaproject2"), config)
    result = remember_tool(
        rt, "x", occurred_at=T1, entity="contact:1", attribute="email", value="v",
        register_new_type=True, register_new_attribute=True,
    )
    assert "event_id" in result
    rt.close()


def test_register_new_attribute_for_an_already_known_type(tmp_path):
    from orlog.errors import SchemaError

    config = OrlogConfig(
        workspace=WorkspaceConfig(name="schemaproject3"),
        retrieval=RetrievalConfig(embedder="hashing"),
        schema_=SchemaConfig(known_types=["user"]),
    )
    rt = Runtime(Workspace(tmp_path / "schemaproject3"), config)
    remember_tool(rt, "x", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    with pytest.raises(SchemaError):
        remember_tool(rt, "x", occurred_at=T1, entity="user:2", attribute="shoe_size", value="42")

    result = remember_tool(rt, "x", occurred_at=T1, entity="user:2", attribute="shoe_size", value="42", register_new_attribute=True)
    assert "event_id" in result
    rt.close()


def test_invalid_occurred_at_string_raises_a_clear_value_error(runtime):
    with pytest.raises(ValueError):
        remember_tool(runtime, text="x", occurred_at="not-a-date", entity="e", attribute="a", value="v")


def test_recall_query_with_more_than_one_dot_splits_on_the_last_one(runtime):
    remember_tool(runtime, "dark theme", occurred_at=T1, entity="user:1.settings", attribute="theme", value="dark")

    result = recall_tool(runtime, "user:1.settings.theme", as_of=T2)

    assert result["verified"] is True
    assert result["claim"] == "user:1.settings.theme = dark"


def test_recall_is_case_sensitive_on_the_exact_key_lookup_path(runtime):
    remember_tool(runtime, "plan is pro", occurred_at=T1, entity="User:1", attribute="Plan", value="pro")

    exact_case = recall_tool(runtime, "User:1.Plan", as_of=T2)
    different_case = recall_tool(runtime, "user:1.plan", as_of=T2)

    assert exact_case["verified"] is True
    assert different_case["abstained"] is True
    assert different_case["reasons"] == ["NO_CANDIDATES"]


def test_recall_tool_ambiguity_margin_boundary_is_inclusive(runtime, monkeypatch):
    remember_tool(runtime, "seed fact", occurred_at=T1, entity="seed", attribute="x", value="y")
    import orlog.server_tools as server_tools_module

    margin = runtime.config.retrieval.ambiguity_margin
    candidates = [
        KeyCandidate(entity="a", attribute="attr", event_id="ev-a", matched_text="a", score=1.0),
        KeyCandidate(entity="b", attribute="attr", event_id="ev-b", matched_text="b", score=1.0 * margin),
    ]
    monkeypatch.setattr(server_tools_module, "resolve_key", lambda *a, **k: candidates)

    result = recall_tool(runtime, "free text query", as_of=T2)

    assert result["reasons"] == ["AMBIGUOUS"]
    assert len(result["candidates"]) == 2


def test_recall_tool_ambiguity_margin_boundary_excludes_a_runner_up_just_below_it(runtime, monkeypatch):
    remember_tool(runtime, "seed fact", occurred_at=T1, entity="seed", attribute="x", value="y")
    import orlog.server_tools as server_tools_module

    margin = runtime.config.retrieval.ambiguity_margin
    candidates = [
        KeyCandidate(entity="a", attribute="attr", event_id="ev-a", matched_text="a", score=1.0),
        KeyCandidate(entity="b", attribute="attr", event_id="ev-b", matched_text="b", score=1.0 * margin - 0.05),
    ]
    monkeypatch.setattr(server_tools_module, "resolve_key", lambda *a, **k: candidates)

    result = recall_tool(runtime, "free text query", as_of=T2)

    assert result["reasons"] != ["AMBIGUOUS"]
```

- [ ] **Step 2: Run to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_server_tools.py -v`
Expected: all tests PASS (existing + 8 new).

- [ ] **Step 3: Commit**

```bash
git add tests/test_server_tools.py
git commit -m "test: add remember/recall edge cases (schema registration, occurred_at, case sensitivity, ambiguity boundary)"
```

---

### Task 5: `tests/test_huginn_llm.py` — more `LLMDeriver.derive()` edge cases

**Files:**
- Modify: `tests/test_huginn_llm.py`

**Interfaces:**
- Consumes: `orlog.huginn_llm.LLMDeriver`, `orlog.huginn.UncitedAssertion` (pre-existing).
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the tests**

Add to `tests/test_huginn_llm.py` (all names already imported — `UncitedAssertion`, `LLMDeriver`, `Candidate`, `pytest`):

```python
def test_duplicate_citation_ids_are_kept_as_is_not_deduplicated():
    completion = _QueuedCompletion(('{"claim": "user:1.plan = pro", "citations": ["ev-1", "ev-1"]}', {"in": 5, "out": 5}))
    deriver = LLMDeriver(completion, model="test-model")

    assertion = deriver.derive("user:1.plan", NOW, [_candidate()], query_id="q1", derived_at=NOW, route="fresh")

    assert assertion.citations == ["ev-1", "ev-1"]


def test_a_citations_field_that_is_a_bare_string_not_a_list_yields_no_valid_citations():
    # `parsed.get("citations") or []` keeps a truthy string as-is; iterating
    # a string yields its individual characters, none of which match a real
    # event id -- so this fails safely into UncitedAssertion rather than
    # silently misbehaving. Pinned down explicitly since it's a subtle
    # consequence of Python's string iteration, not an intentional check.
    completion = _QueuedCompletion(('{"claim": "user:1.plan = pro", "citations": "ev-1"}', {"in": 5, "out": 5}))
    deriver = LLMDeriver(completion, model="test-model")

    with pytest.raises(UncitedAssertion):
        deriver.derive("user:1.plan", NOW, [_candidate()], query_id="q1", derived_at=NOW, route="fresh")


def test_empty_string_claim_is_treated_the_same_as_a_missing_claim():
    completion = _QueuedCompletion(('{"claim": "", "citations": ["ev-1"]}', {"in": 5, "out": 5}))
    deriver = LLMDeriver(completion, model="test-model")

    with pytest.raises(UncitedAssertion):
        deriver.derive("user:1.plan", NOW, [_candidate()], query_id="q1", derived_at=NOW, route="fresh")


def test_a_brace_inside_a_quoted_string_value_does_not_break_extraction():
    text = 'Here is my answer: {"claim": "the value is {nested}", "citations": ["ev-1"]} - hope that helps!'
    completion = _QueuedCompletion((text, {"in": 5, "out": 5}))
    deriver = LLMDeriver(completion, model="test-model")

    assertion = deriver.derive("user:1.plan", NOW, [_candidate()], query_id="q1", derived_at=NOW, route="fresh")

    assert assertion.claim == "the value is {nested}"
    assert assertion.citations == ["ev-1"]


def test_json_wrapped_in_a_markdown_code_fence_extracts_on_the_first_try():
    fenced = '```json\n{"claim": "user:1.plan = pro", "citations": ["ev-1"]}\n```'
    completion = _QueuedCompletion((fenced, {"in": 5, "out": 5}))
    deriver = LLMDeriver(completion, model="test-model")

    assertion = deriver.derive("user:1.plan", NOW, [_candidate()], query_id="q1", derived_at=NOW, route="fresh")

    assert assertion.claim == "user:1.plan = pro"
    assert completion.calls == 1  # no repair retry needed
```

- [ ] **Step 2: Run to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_huginn_llm.py -v`
Expected: all tests PASS (existing 8 + 5 new).

- [ ] **Step 3: Commit**

```bash
git add tests/test_huginn_llm.py
git commit -m "test: add LLMDeriver edge cases (duplicate/malformed citations, empty claim, brace/fence extraction)"
```

---

### Task 6: `tests/conformance/test_c1_supersession.py` — same-instant tie-break

**Files:**
- Modify: `tests/conformance/test_c1_supersession.py`

**Interfaces:**
- Consumes: everything already imported in this file.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the test**

Add to `tests/conformance/test_c1_supersession.py`:

```python
def test_two_facts_at_the_exact_same_instant_resolve_deterministically(make_log, make_event):
    # verdandi's sort key is (occurred_at, recorded_at, id) -- with the
    # fixed test clock every make_log() event shares the same recorded_at
    # too, so ties resolve by id (insertion order, for monotonically
    # generated ULIDs). The FIRST fact's validity window collapses to
    # zero-width [T1, T1) and becomes permanently unreachable; the SECOND
    # wins at exactly T1, deterministically -- not by insertion-order luck.
    log = make_log()
    log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "plan", "value": "first"}))
    log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "plan", "value": "second"}))

    pipeline = _build_pipeline(log)

    result = pipeline.answer("q-tie", "user:1", "plan", T1, now=NOW)
    assert result.verified is True
    assert result.claim == "user:1.plan = second"
```

- [ ] **Step 2: Run to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/conformance/test_c1_supersession.py -v`
Expected: all 4 tests PASS (existing 3 + 1 new).

- [ ] **Step 3: Commit**

```bash
git add tests/conformance/test_c1_supersession.py
git commit -m "test: C1 same-instant supersession tie-break is deterministic"
```

---

### Task 7: `tests/conformance/test_c2_cache_staleness.py` — per-key eviction leaves neighbors cached

**Files:**
- Modify: `tests/conformance/test_c2_cache_staleness.py`

**Interfaces:**
- Consumes: everything already imported in this file.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the test**

Add to `tests/conformance/test_c2_cache_staleness.py`:

```python
T_BETWEEN = datetime(2026, 3, 1, tzinfo=timezone.utc)


def test_a_verification_failure_evicts_only_its_own_key_not_a_neighbors(make_log, make_event):
    # Distinct from the truth_version-rotation test above: this isolates
    # the OTHER invalidation path pipeline.answer() has (a cached
    # assertion that fails re-verification gets evicted on its own,
    # spec §B6) by rebuilding the verifier from new events while
    # deliberately keeping truth_version as the SAME literal string --
    # proving reuse is genuinely per-key, not merely "whatever wasn't
    # touched by the last truth_version bump."
    log = make_log()
    log.append(make_event(occurred_at=T1, payload={"entity": "user:1", "attribute": "status", "value": "active"}))
    log.append(make_event(occurred_at=T1, payload={"entity": "user:2", "attribute": "status", "value": "active"}))

    cache = RouteCache()
    pipeline = _build_pipeline(log, cache)

    r1 = pipeline.answer("q1", "user:1", "status", T2, now=T1)
    r2 = pipeline.answer("q2", "user:2", "status", T2, now=T1)
    assert r1.route == "fresh" and r2.route == "fresh"

    log.append(make_event(occurred_at=T_BETWEEN, payload={"entity": "user:1", "attribute": "status", "value": "inactive"}))
    events = log.read_all()
    view, pv = build_supersession_chains(events, builder="test", built_at=T1)
    pipeline.view = view
    pipeline.projection_version = pv
    pipeline.events_by_id = {e.id: e for e in events}
    pipeline.verifier = Heimdall(build_ground_truth(events), truth_version="v-initial")

    r1b = pipeline.answer("q1b", "user:1", "status", T2, now=T2)
    r2b = pipeline.answer("q2b", "user:2", "status", T2, now=T2)

    assert r1b.verified is True and r1b.claim == "user:1.status = inactive"  # re-derived, not stale
    assert r2b.route == "cache"  # untouched neighbor: still served straight from cache
    assert r2b.claim == "user:2.status = active"
```

- [ ] **Step 2: Run to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/conformance/test_c2_cache_staleness.py -v`
Expected: all 3 tests PASS (existing 2 + 1 new).

- [ ] **Step 3: Commit**

```bash
git add tests/conformance/test_c2_cache_staleness.py
git commit -m "test: C2 a per-key verification failure never evicts a neighbor's cache entry"
```

---

### Task 8: `tests/conformance/test_c3_imperfect_projection.py` — exact 30%/90% boundary

**Files:**
- Modify: `tests/conformance/test_c3_imperfect_projection.py`

**Interfaces:**
- Consumes: everything already imported in this file.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the test**

Add to `tests/conformance/test_c3_imperfect_projection.py`:

```python
def test_corruption_at_exactly_the_documented_30_percent_boundary_still_meets_the_90_percent_floor(make_log, make_event):
    # The existing test above corrupts 4 of 10 (40%, per its own CORRUPT_EVERY
    # comment) -- this pins down the literal boundary the spec table states
    # (30%/90%) rather than only a comfortably-inside-the-margin case.
    log = _build_log(make_log, make_event)
    events = log.read_all()

    view, pv = build_supersession_chains(events, builder="test", built_at=NOW)
    truth = build_ground_truth(events)

    corrupted_entities = {0, 1, 2}  # exactly 3 of 10 == 30%
    corrupted_keys = {f"user:{i}::plan" for i in corrupted_entities}
    view = _corrupt_chains(view, corrupted_keys)

    pipeline = Pipeline(
        view=view,
        events_by_id={e.id: e for e in events},
        projection_version=pv,
        cache=RouteCache(),
        deriver=ScriptedDeriver(),
        verifier=Heimdall(truth, truth_version="test-v1"),
        ledger=OutcomeLedger(log),
    )

    wrong_answers_served = []
    correct_on_uncorrupted = 0
    for i in range(ENTITY_COUNT):
        result = pipeline.answer(f"q{i}", f"user:{i}", "plan", NOW, now=NOW)
        expected = f"user:{i}.plan = plan-{i}"
        if i in corrupted_entities:
            if result.verified and result.claim != expected:
                wrong_answers_served.append((i, result.claim))
        elif result.verified and result.claim == expected:
            correct_on_uncorrupted += 1

    assert wrong_answers_served == []
    uncorrupted_count = ENTITY_COUNT - len(corrupted_entities)
    assert correct_on_uncorrupted / uncorrupted_count >= 0.90
```

- [ ] **Step 2: Run to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/conformance/test_c3_imperfect_projection.py -v`
Expected: both tests PASS (existing 1 + 1 new).

- [ ] **Step 3: Commit**

```bash
git add tests/conformance/test_c3_imperfect_projection.py
git commit -m "test: C3 corruption at the exact documented 30%/90% boundary"
```

---

### Task 9: `tests/conformance/test_c5_immutability_and_replay.py` — replay across a segment rotation

**Files:**
- Modify: `tests/conformance/test_c5_immutability_and_replay.py`

**Interfaces:**
- Consumes: `orlog.storage.SegmentedLog`, `orlog.models.event.EventDraft` (new imports for this file; both pre-existing in the codebase).
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the test**

Add to `tests/conformance/test_c5_immutability_and_replay.py` (add `from orlog.storage import SegmentedLog` and `from orlog.models.event import EventDraft` to the existing imports at the top):

```python
def test_replay_is_still_byte_identical_across_a_segment_rotation_boundary(tmp_path):
    # Every other C5 test in this file uses a single-segment EventLog (via
    # make_log). This proves replay determinism survives an ACTUAL
    # multi-segment log -- rotate_bytes=200 matches the convention already
    # used in tests/test_storage.py to force rotation without writing 64MB.
    clock = lambda: BUILT_AT  # noqa: E731
    log = SegmentedLog(tmp_path / "events", clock=clock, rotate_bytes=200)
    log.append(EventDraft(occurred_at=T1, actor="test", type="fact", payload={"entity": "user:1", "attribute": "plan", "value": "free", "padding": "x" * 80}))
    log.append(EventDraft(occurred_at=T2, actor="test", type="fact", payload={"entity": "user:1", "attribute": "plan", "value": "pro", "padding": "x" * 80}))

    segments = sorted((tmp_path / "events").glob("log-*.jsonl"))
    assert len(segments) > 1  # rotation actually happened
    assert log.verify_chain(full=True) is True

    view_a, pv_a = build_supersession_chains(log.read_all(), builder="test", built_at=BUILT_AT, version=1)
    view_b, pv_b = build_supersession_chains(log.read_all(), builder="test", built_at=BUILT_AT, version=1)

    assert view_a.model_dump_json() == view_b.model_dump_json()
    assert pv_a.model_dump_json() == pv_b.model_dump_json()
```

- [ ] **Step 2: Run to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/conformance/test_c5_immutability_and_replay.py -v`
Expected: all 4 tests PASS (existing 3 + 1 new).

- [ ] **Step 3: Commit**

```bash
git add tests/conformance/test_c5_immutability_and_replay.py
git commit -m "test: C5 replay determinism survives a segment rotation boundary"
```

---

### Task 10: `tests/integration/` harness + fuzzy-text-to-schema scenarios 1-3

**Files:**
- Create: `tests/integration/test_remember_extraction.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `orlog.server.build_server`, `orlog.runtime.Runtime`, `orlog.config.*`, `orlog.workspace.Workspace`, `orlog.vault.generate_key` (all pre-existing); the real `anthropic` SDK (installed, not mocked, in this task only).
- Produces: `_anthropic_tools(mcp)`, `_ask_model_to_call_a_tool(mcp, user_message) -> (name, input)`, `_execute_tool_call(mcp, name, arguments) -> dict`, the `runtime`/`mcp` fixtures — Task 11 appends more test functions to this same file and reuses all four.

- [ ] **Step 1: Register the `integration` marker**

In `pyproject.toml`, change:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
```

to:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "integration: makes real Anthropic API calls; requires ANTHROPIC_API_KEY, skipped otherwise",
]
```

- [ ] **Step 2: Write the harness + first 3 scenarios**

Create `tests/integration/test_remember_extraction.py`:

```python
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
```

- [ ] **Step 3: Verify the module skips cleanly without a key**

Run: `./.venv/Scripts/python.exe -m pytest tests/integration/ -v`
Expected: 3 tests, all SKIPPED (not failed, not errored) — confirms the rest of the suite stays unaffected by anyone without `ANTHROPIC_API_KEY` set.

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: same pass count as before this task plus the 3 new tests reported as skipped (211 + everything from Tasks 1-9 + 3 skipped).

- [ ] **Step 4: If you have a real `ANTHROPIC_API_KEY`, run it for real**

Run: `ANTHROPIC_API_KEY=<your-key> ./.venv/Scripts/python.exe -m pytest tests/integration/test_remember_extraction.py -v`
Expected: all 3 tests PASS. If a specific assertion fails, re-read it: the assertions are intentionally fuzzy (substring, case-insensitive) — a genuine failure here means the model's tool call didn't include the expected entity/value substring at all, which is worth reporting (it may mean `server.py`'s descriptions need work), not silently loosening the assertion until it passes.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_remember_extraction.py pyproject.toml
git commit -m "test: add live LLM tool-calling integration tests (fuzzy text -> remember schema)"
```

---

### Task 11: fuzzy-text-to-schema scenarios 4-5 (disambiguation, reverse recall)

**Files:**
- Modify: `tests/integration/test_remember_extraction.py`

**Interfaces:**
- Consumes: `_ask_model_to_call_a_tool`, `_execute_tool_call`, the `mcp`/`runtime` fixtures (all produced by Task 10, same file).
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the two remaining scenarios**

Add to `tests/integration/test_remember_extraction.py`:

```python
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
```

- [ ] **Step 2: Verify the module still skips cleanly without a key**

Run: `./.venv/Scripts/python.exe -m pytest tests/integration/ -v`
Expected: 5 tests, all SKIPPED.

- [ ] **Step 3: If you have a real `ANTHROPIC_API_KEY`, run it for real**

Run: `ANTHROPIC_API_KEY=<your-key> ./.venv/Scripts/python.exe -m pytest tests/integration/test_remember_extraction.py -v`
Expected: all 5 tests PASS.

- [ ] **Step 4: Final full-suite regression check**

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: every Part A test (Tasks 1-9) passes; Part B tests (Task 10-11) report as skipped without a key, or pass with one.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_remember_extraction.py
git commit -m "test: add disambiguation and reverse-recall integration scenarios"
```

---

## Final report checklist (for whoever executes this plan)

When all 11 tasks are done, explicitly call out to the project owner:

1. **The empty-string `remember()` bug** (Task 1) was found and fixed as part of this work, per their explicit approval during planning — not a silent side effect.
2. **A real documentation gap**, found but deliberately NOT fixed (Task 2): `recall`'s tool description in `server.py` documents `NO_CANDIDATES`/`AMBIGUOUS`/`EMBEDDER_UNAVAILABLE`/`UNSUPPORTED_BY_SOURCE`/`VERIFY_TIMEOUT`, but never mentions `UNCITED`/`NOT_FOUND`/`NOT_YET_VALID`/`EXPIRED` (heimdall verification-failure codes) or `DERIVER_INSUFFICIENT`/`DERIVER_TIMEOUT` (deriver-failure reasons) — all of which can also reach a calling LLM via `Answer.reasons`. Worth a follow-up decision on whether to document them.
3. Whether Part B (`tests/integration/`) should ever run in CI is an open, separate decision — it would require adding `ANTHROPIC_API_KEY` as a repository secret, which this plan deliberately does not do.
