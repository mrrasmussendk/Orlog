"""Contract: the event-type registry (ORLOG-SPEC.md §B12.1).

A profile registers `x.<vendor>.*` types with pydantic payload models;
remember() takes a typed payload for those and keeps the
entity/attribute/value requirement for "fact".
"""

from datetime import datetime, timezone

import pytest
from pydantic import BaseModel, ConfigDict, Field

from orlog.config import OrlogConfig, RetrievalConfig, SchemaConfig, WorkspaceConfig
from orlog.errors import SchemaError
from orlog.event_types import CORE_EVENT_TYPES, EventTypeRegistry, EventTypeSpec
from orlog.plugins import EVENT_TYPES_GROUP, PluginError, clear_registered, register
from orlog.runtime import Runtime
from orlog.vault import generate_key
from orlog.workspace import Workspace


class DecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=1)
    rationale: str = ""
    scope: str = "*"


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    monkeypatch.setenv("ORLOG_VAULT_KEY", generate_key())


@pytest.fixture(autouse=True)
def clean_registry():
    clear_registered()
    yield
    clear_registered()


@pytest.fixture
def registered_decision():
    register(EVENT_TYPES_GROUP, "demo", {"x.demo.decision": DecisionPayload}, distribution="demo")
    return "x.demo.decision"


@pytest.fixture
def runtime(tmp_path, registered_decision):
    runtime = Runtime(
        Workspace(tmp_path / "ws"),
        OrlogConfig(
            workspace=WorkspaceConfig(name="types"), retrieval=RetrievalConfig(embedder="hashing")
        ),
    )
    yield runtime
    runtime.close()


# -- namespace rules -------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["decision", "mimir.decision", "x.decision", "X.Demo.Decision", "x.demo-vendor.decision", ""],
)
def test_a_type_name_outside_the_x_vendor_namespace_is_refused(name):
    register(EVENT_TYPES_GROUP, "demo", {name: DecisionPayload}, distribution="demo")
    with pytest.raises(PluginError) as exc:
        EventTypeRegistry.load()
    assert "vendor-namespaced" in str(exc.value)


def test_the_orlog_vendor_is_reserved():
    register(EVENT_TYPES_GROUP, "demo", {"x.orlog.sneaky": DecisionPayload}, distribution="demo")
    with pytest.raises(PluginError, match="reserved"):
        EventTypeRegistry.load()


@pytest.mark.parametrize("name", sorted(CORE_EVENT_TYPES))
def test_a_core_type_cannot_be_overridden(name):
    register(EVENT_TYPES_GROUP, "demo", {name: DecisionPayload}, distribution="demo")
    with pytest.raises(PluginError, match="not overridable"):
        EventTypeRegistry.load()


def test_two_profiles_cannot_claim_the_same_type_name():
    register(EVENT_TYPES_GROUP, "a", {"x.demo.decision": DecisionPayload}, distribution="a")
    register(EVENT_TYPES_GROUP, "b", {"x.demo.decision": DecisionPayload}, distribution="b")
    with pytest.raises(PluginError, match="registered twice"):
        EventTypeRegistry.load()


def test_a_payload_model_must_be_a_pydantic_model():
    register(EVENT_TYPES_GROUP, "demo", {"x.demo.decision": dict}, distribution="demo")
    with pytest.raises(PluginError, match="BaseModel"):
        EventTypeRegistry.load()


# -- the merged known-type set ---------------------------------------------


def test_known_event_types_merges_core_the_registry_and_config(registered_decision):
    known = EventTypeRegistry.load().known_event_types(["x.workspace.local"])
    assert CORE_EVENT_TYPES <= known
    assert "x.demo.decision" in known
    assert "x.workspace.local" in known


# -- remember() ------------------------------------------------------------


def test_a_registered_type_is_written_with_a_payload(runtime, registered_decision):
    event = runtime.remember(
        payload={"claim": "pin mcp below 2", "rationale": "FastMCP was renamed"},
        event_type=registered_decision,
    )
    assert event.type == registered_decision
    assert event.payload == {
        "claim": "pin mcp below 2",
        "rationale": "FastMCP was renamed",
        "scope": "*",
    }


def test_an_invalid_payload_is_refused_at_write_time(runtime, registered_decision):
    with pytest.raises(SchemaError) as exc:
        runtime.remember(payload={"rationale": "no claim"}, event_type=registered_decision)
    assert "claim" in str(exc.value)
    # An append-only log cannot be corrected afterwards, so the write must
    # not have happened at all.
    assert runtime.log.event_count() == 0


def test_an_unknown_event_type_is_refused_rather_than_silently_logged(runtime):
    with pytest.raises(SchemaError, match="not a known event type"):
        runtime.remember("hi", event_type="x.typo.decsion", entity="a", attribute="b", value="hi")


def test_a_type_declared_in_config_is_accepted_without_a_profile(tmp_path):
    runtime = Runtime(
        Workspace(tmp_path / "declared"),
        OrlogConfig(
            workspace=WorkspaceConfig(name="declared"),
            retrieval=RetrievalConfig(embedder="hashing"),
            schema_=SchemaConfig(known_event_types=["x.local.note"]),
        ),
    )
    try:
        # Declared but unregistered: there is no model, so it is still
        # written through core's fact shape, not as a typed payload.
        event = runtime.remember(
            "a local note", event_type="x.local.note", entity="n:1", attribute="body", value="a local note"
        )
        assert event.type == "x.local.note"
    finally:
        runtime.close()


def test_payload_is_refused_for_a_type_that_has_no_model(runtime):
    with pytest.raises(SchemaError, match="only accepted for a registered event type"):
        runtime.remember(payload={"claim": "x"}, event_type="fact")


def test_text_is_refused_on_a_typed_write(runtime, registered_decision):
    # A typed event's content is its payload. Accepting `text` too would
    # mean two places for the same information and one of them silently
    # dropped.
    with pytest.raises(SchemaError, match="carries its content in `payload`"):
        runtime.remember("prose", payload={"claim": "x"}, event_type=registered_decision)


def test_fact_still_requires_entity_attribute_and_value(runtime):
    with pytest.raises(SchemaError, match="requires entity, attribute, and value"):
        runtime.remember("just text")


def test_a_typed_payload_is_scrubbed_before_it_reaches_the_log(runtime, registered_decision):
    event = runtime.remember(
        payload={"claim": "mail alice@example.com", "rationale": "see alice@example.com"},
        event_type=registered_decision,
    )
    # Scrubbed, so `orlog forget` can still reach it -- and tokenized
    # consistently across fields, since vault.tokenize is stable per input.
    assert "alice@example.com" not in event.payload["claim"]
    assert "alice@example.com" not in event.payload["rationale"]
    token = event.payload["claim"].split()[-1]
    assert event.payload["rationale"].endswith(token)


def test_a_typed_event_is_indexed_and_queryable_by_type(runtime, registered_decision):
    runtime.remember(payload={"claim": "one"}, event_type=registered_decision)
    runtime.remember(payload={"claim": "two"}, event_type=registered_decision)
    found = runtime.query.events(type_prefix="x.demo.")
    assert [e.payload["claim"] for e in found] == ["one", "two"]


def test_a_typed_event_does_not_disturb_the_fact_pipeline(runtime, registered_decision):
    runtime.remember("plan is pro", entity="user:1", attribute="plan", value="plan is pro")
    runtime.remember(payload={"claim": "unrelated"}, event_type=registered_decision)
    pipeline = runtime.build_pipeline(now=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert list(pipeline.view.chains) == ["user:1::plan"]


# -- the MCP surface, not just Runtime -------------------------------------


def test_remember_tool_accepts_a_typed_payload(runtime, registered_decision):
    """Runtime.remember() grew a `payload` argument; the tool in front of it
    has to grow one too, or the seam exists only for callers who have
    already imported orlog -- which is every caller except the MCP client
    the seam is for."""
    from orlog.server_tools import remember_tool

    result = remember_tool(runtime, type=registered_decision, payload={"claim": "pin mcp below 2"})

    (event,) = runtime.query.events(types=[registered_decision])
    assert result == {"event_id": event.id}
    assert event.payload["claim"] == "pin mcp below 2"


def test_remember_over_the_real_mcp_surface_takes_a_typed_payload(runtime, registered_decision):
    import anyio

    from orlog.server import build_server

    server = build_server(runtime)
    (tool,) = [t for t in anyio.run(server.list_tools) if t.name == "remember"]
    assert "payload" in tool.inputSchema["properties"]
    assert "text" not in tool.inputSchema.get("required", [])

    anyio.run(
        lambda: server.call_tool(
            "remember", {"type": registered_decision, "payload": {"claim": "over mcp"}}
        )
    )
    (event,) = runtime.query.events(types=[registered_decision])
    assert event.payload["claim"] == "over mcp"


def test_remember_over_mcp_still_requires_eav_for_a_fact(runtime):
    import anyio
    from mcp.server.fastmcp.exceptions import ToolError

    from orlog.server import build_server

    server = build_server(runtime)
    with pytest.raises(ToolError) as exc:
        anyio.run(lambda: server.call_tool("remember", {"text": "just text"}))
    assert "entity, attribute, and value" in str(exc.value)
