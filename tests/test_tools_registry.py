"""Contract: the MCP tool registry (ORLOG-SPEC.md §B12.3)."""

import inspect

import anyio
import pytest

from orlog.config import OrlogConfig, ProfilesConfig, RetrievalConfig, WorkspaceConfig
from orlog.plugins import TOOLS_GROUP, PluginError, clear_registered, register
from orlog.runtime import Runtime
from orlog.server import build_server
from orlog.tools import CORE_TOOL_NAMES, ToolRegistry, ToolSpec, as_specs, bind
from orlog.vault import generate_key
from orlog.workspace import Workspace


def echo(runtime, message: str, times: int = 1) -> dict:
    """Echo a message back. Reference tool."""
    return {"echo": message * times, "workspace": runtime.config.workspace.name}


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    monkeypatch.setenv("ORLOG_VAULT_KEY", generate_key())


@pytest.fixture(autouse=True)
def clean_registry():
    clear_registered()
    yield
    clear_registered()


@pytest.fixture
def runtime(tmp_path):
    runtime = Runtime(
        Workspace(tmp_path / "ws"),
        OrlogConfig(
            workspace=WorkspaceConfig(name="tools"), retrieval=RetrievalConfig(embedder="hashing")
        ),
    )
    yield runtime
    runtime.close()


# -- what a profile may register -------------------------------------------


@pytest.mark.parametrize("name", sorted(CORE_TOOL_NAMES))
def test_a_core_tool_name_cannot_be_shadowed(name):
    register(TOOLS_GROUP, "evil", {name: echo}, distribution="evil")
    # Shadowing `recall` would replace the one call carrying the
    # verify-or-abstain guarantee, decided by entry-point ordering.
    with pytest.raises(PluginError, match="not overridable"):
        ToolRegistry.load()


def test_a_tool_must_take_the_runtime_first():
    def no_runtime(**kwargs) -> dict:
        return {}

    register(TOOLS_GROUP, "bad", {"bad": no_runtime}, distribution="bad")
    with pytest.raises(PluginError, match="first parameter must be the Runtime"):
        ToolRegistry.load()


def test_a_tool_name_must_be_callable_by_name():
    register(TOOLS_GROUP, "bad", {"not a name": echo}, distribution="bad")
    with pytest.raises(PluginError, match="valid identifier"):
        ToolRegistry.load()


def test_two_profiles_cannot_claim_the_same_tool_name():
    register(TOOLS_GROUP, "a", {"echo": echo}, distribution="a")
    register(TOOLS_GROUP, "b", {"echo": echo}, distribution="b")
    with pytest.raises(PluginError, match="registered twice"):
        ToolRegistry.load()


# -- binding ---------------------------------------------------------------


def test_bind_hides_the_runtime_parameter_and_keeps_the_rest(runtime):
    bound = bind(ToolSpec(name="echo", func=echo, distribution="test"), runtime)

    assert str(inspect.signature(bound)) == "(message: str, times: int = 1) -> dict"
    assert bound.__name__ == "echo"
    assert bound.__doc__.startswith("Echo a message back")
    assert bound(message="hi", times=2)["echo"] == "hihi"
    assert bound(message="hi")["workspace"] == "tools"


def test_bind_resolves_annotations_in_the_profiles_own_module(runtime):
    """A tool annotated with a type that exists only in its own module must
    still produce a schema -- FastMCP would otherwise try to resolve the
    name here, where it does not exist."""
    namespace: dict = {}
    exec(
        "from __future__ import annotations\n"
        "class Topic(str):\n    pass\n"
        "def t(runtime, topic: Topic) -> dict:\n    return {'topic': topic}\n",
        namespace,
    )
    bound = bind(ToolSpec(name="t", func=namespace["t"], distribution="test"), runtime)
    assert inspect.signature(bound).parameters["topic"].annotation is namespace["Topic"]


# -- build_server ----------------------------------------------------------


def test_extra_tools_are_served_alongside_cores(runtime):
    server = build_server(runtime, {"echo": echo})
    names = {tool.name for tool in anyio.run(server.list_tools)}

    assert CORE_TOOL_NAMES <= names
    assert "echo" in names


def test_an_extra_tool_is_callable_over_the_real_mcp_surface(runtime):
    server = build_server(runtime, {"echo": echo})
    result = anyio.run(lambda: server.call_tool("echo", {"message": "hi", "times": 3}))
    assert "hihihi" in str(result)


def test_an_extra_tools_description_comes_from_its_docstring(runtime):
    server = build_server(runtime, {"echo": echo})
    (tool,) = [t for t in anyio.run(server.list_tools) if t.name == "echo"]
    assert tool.description.startswith("Echo a message back")
    assert "message" in tool.inputSchema["properties"]
    assert tool.inputSchema["required"] == ["message"]


def test_an_extra_tool_cannot_shadow_a_core_one(runtime):
    with pytest.raises(PluginError, match="not overridable"):
        build_server(runtime, {"recall": echo})


def test_entry_point_tools_and_extra_tools_are_both_served(runtime):
    register(TOOLS_GROUP, "demo", {"echo": echo}, distribution="demo")

    def other(runtime, x: str = "") -> dict:
        """Other."""
        return {"x": x}

    names = {tool.name for tool in anyio.run(build_server(runtime, {"other": other}).list_tools)}
    assert {"echo", "other"} <= names


def test_a_workspace_that_disables_a_profile_does_not_serve_its_tools(tmp_path):
    register(TOOLS_GROUP, "demo", {"echo": echo}, distribution="demo")
    runtime = Runtime(
        Workspace(tmp_path / "off"),
        OrlogConfig(
            workspace=WorkspaceConfig(name="off"),
            retrieval=RetrievalConfig(embedder="hashing"),
            profiles=ProfilesConfig(disabled=["demo"]),
        ),
    )
    try:
        names = {tool.name for tool in anyio.run(build_server(runtime).list_tools)}
        assert "echo" not in names
    finally:
        runtime.close()


def test_as_specs_rejects_something_that_is_not_a_tool():
    with pytest.raises(PluginError, match="ToolSpec"):
        as_specs(["not a tool"])
