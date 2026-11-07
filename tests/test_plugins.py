"""Contract: a profile extends orlog through entry points, without a fork
(ORLOG-SPEC.md §B12).

§B12's acceptance criterion is a reference plugin that registers one event
type, one projection and one tool through entry points, and passes. That is
`tests/fixtures/orlog_refplugin`, and `installed_refplugin` below installs
it the way pip would: a real .dist-info with an entry_points.txt, on
sys.path, discovered by importlib.metadata. Nothing here monkeypatches
discovery -- a test that fakes the mechanism it is testing proves nothing
about whether a real `pip install` would work.
"""

from __future__ import annotations

import importlib
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orlog.config import OrlogConfig, ProfilesConfig, RetrievalConfig, WorkspaceConfig
from orlog.errors import SchemaError
from orlog.event_types import EventTypeRegistry
from orlog.plugins import (
    EVENT_TYPES_GROUP,
    PROJECTIONS_GROUP,
    TOOLS_GROUP,
    PluginError,
    load_group,
)
from orlog.projections import ProjectionRegistry
from orlog.runtime import Runtime
from orlog.server_tools import remember_tool
from orlog.tools import ToolRegistry
from orlog.vault import generate_key
from orlog.workspace import Workspace

FIXTURE = Path(__file__).parent / "fixtures" / "orlog_refplugin"
DIST_NAME = "orlog-refplugin"

ENTRY_POINTS = f"""\
[{EVENT_TYPES_GROUP}]
refplugin = orlog_refplugin:EVENT_TYPES

[{PROJECTIONS_GROUP}]
refplugin = orlog_refplugin:PROJECTIONS

[{TOOLS_GROUP}]
refplugin = orlog_refplugin:TOOLS
"""


@pytest.fixture
def installed_refplugin(tmp_path, monkeypatch):
    """Install tests/fixtures/orlog_refplugin as a real distribution in a
    temporary site directory and put it on sys.path.

    importlib.metadata finds entry points by walking sys.path for
    .dist-info directories, so this is the same code path a pip-installed
    profile takes -- which is the whole point of the acceptance criterion.
    """
    site = tmp_path / "site"
    shutil.copytree(FIXTURE, site / "orlog_refplugin")
    dist_info = site / f"{DIST_NAME.replace('-', '_')}-0.1.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {DIST_NAME}\nVersion: 0.1.0\n", encoding="utf-8"
    )
    (dist_info / "entry_points.txt").write_text(ENTRY_POINTS, encoding="utf-8")

    monkeypatch.syspath_prepend(str(site))
    importlib.invalidate_caches()
    yield DIST_NAME
    sys.modules.pop("orlog_refplugin", None)


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    monkeypatch.setenv("ORLOG_VAULT_KEY", generate_key())


def _config(**profiles) -> OrlogConfig:
    return OrlogConfig(
        workspace=WorkspaceConfig(name="plugins"),
        retrieval=RetrievalConfig(embedder="hashing"),
        profiles=ProfilesConfig(**profiles),
    )


@pytest.fixture
def runtime(tmp_path, installed_refplugin):
    ws = Workspace(tmp_path / "ws")
    runtime = Runtime(ws, _config())
    yield runtime
    runtime.close()


# -- discovery -------------------------------------------------------------


def test_all_three_groups_are_discovered_through_real_entry_points(installed_refplugin):
    for group in (EVENT_TYPES_GROUP, PROJECTIONS_GROUP, TOOLS_GROUP):
        loaded = load_group(group)
        assert DIST_NAME in [e.distribution for e in loaded], group


def test_the_reference_plugin_registers_one_type_one_projection_and_one_tool(installed_refplugin):
    # Scoped with the allowlist rather than asserting a global set: an
    # orlog profile installed in the same interpreter is a perfectly
    # ordinary thing for a developer to have, and this suite must not
    # depend on whether they do.
    only = {"enabled": [DIST_NAME]}
    assert EventTypeRegistry.load(**only).names == {"x.refplugin.note"}
    # Core's own two projections are always there; the plugin adds a third.
    assert ProjectionRegistry.load(**only).names == (
        "supersession_chains",
        "session_summaries",
        "notes_by_topic",
    )
    assert ToolRegistry.load(**only).names == ("notes_about",)


def test_nothing_is_discovered_when_the_profile_is_not_installed():
    assert DIST_NAME not in [e.distribution for e in load_group(EVENT_TYPES_GROUP)]
    assert "x.refplugin.note" not in EventTypeRegistry.load().names
    assert "notes_about" not in ToolRegistry.load().names


# -- the workspace decides which profiles load ------------------------------


def test_enabled_is_a_strict_allowlist(installed_refplugin):
    assert EventTypeRegistry.load(enabled=[DIST_NAME]).names == {"x.refplugin.note"}
    # A workspace that allowlists some OTHER distribution gets nothing from
    # this one, however installed it is -- the allowlist is not a hint.
    assert "x.refplugin.note" not in EventTypeRegistry.load(enabled=["something-else"]).names


def test_disabled_subtracts_from_the_default(installed_refplugin):
    assert "x.refplugin.note" not in EventTypeRegistry.load(disabled=[DIST_NAME]).names


def test_a_disabled_profiles_types_are_not_writable(tmp_path, installed_refplugin):
    ws = Workspace(tmp_path / "off")
    runtime = Runtime(ws, _config(disabled=[DIST_NAME]))
    try:
        with pytest.raises(SchemaError) as exc:
            runtime.remember(payload={"body": "hi"}, event_type="x.refplugin.note")
        assert "not a known event type" in str(exc.value)
    finally:
        runtime.close()


# -- a broken profile fails visibly -----------------------------------------


def test_an_entry_point_that_cannot_be_imported_names_the_distribution(tmp_path, monkeypatch):
    site = tmp_path / "site"
    (site / "brokenplugin").mkdir(parents=True)
    (site / "brokenplugin" / "__init__.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    dist_info = site / "brokenplugin-0.1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: brokenplugin\nVersion: 0.1.0\n", encoding="utf-8"
    )
    (dist_info / "entry_points.txt").write_text(
        f"[{EVENT_TYPES_GROUP}]\nbroken = brokenplugin:TYPES\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(site))
    importlib.invalidate_caches()

    # Silently skipping a profile that failed to load would leave a
    # workspace quietly missing every memory that profile was meant to
    # type -- E_PLUGIN, naming who to go and fix.
    with pytest.raises(PluginError) as exc:
        EventTypeRegistry.load()
    assert "brokenplugin" in str(exc.value)
    sys.modules.pop("brokenplugin", None)


# -- end to end through a Runtime -------------------------------------------


def test_a_profile_type_round_trips_through_remember_and_the_index(runtime):
    event = runtime.remember(
        payload={"body": "ship the seams first", "topic": "plan"},
        event_type="x.refplugin.note",
    )
    assert event.type == "x.refplugin.note"
    assert event.payload == {"body": "ship the seams first", "topic": "plan"}
    assert [e.id for e in runtime.query.events(types=["x.refplugin.note"])] == [event.id]


def test_a_profile_projection_is_built_from_the_log(runtime):
    first = runtime.remember(payload={"body": "a", "topic": "plan"}, event_type="x.refplugin.note")
    second = runtime.remember(payload={"body": "b", "topic": "risk"}, event_type="x.refplugin.note")

    built = runtime.projection_store.rebuild_all(
        runtime.query.events(), built_at=datetime(2030, 1, 1, tzinfo=timezone.utc)
    )
    assert built["notes_by_topic"].builder == "orlog-refplugin"
    view = runtime.projection_store.view(
        "notes_by_topic", runtime.query.events(), built_at=datetime(2030, 1, 1, tzinfo=timezone.utc)
    ).view
    assert view.topics == {"plan": [first.id], "risk": [second.id]}


def test_a_profile_tool_is_served_alongside_cores(runtime):
    runtime.remember(payload={"body": "a", "topic": "plan"}, event_type="x.refplugin.note")
    remember_tool(runtime, "plan is pro", entity="user:1", attribute="plan", value="pro")

    from orlog.server import build_server

    server = build_server(runtime)
    import anyio

    names = {tool.name for tool in anyio.run(server.list_tools)}
    assert "notes_about" in names
    assert {"remember", "recall", "stats"} <= names

    spec = ToolRegistry.load().get("notes_about")
    assert spec.func(runtime, topic="plan")["notes"][0]["body"] == "a"


def test_enabled_is_three_state(installed_refplugin, tmp_path):
    """Absent means every installed profile; an explicit empty list means
    none. Reading [] as "all" would leave a workspace no way to refuse
    third-party code at all."""
    assert "x.refplugin.note" in EventTypeRegistry.load().names            # absent -> all
    assert EventTypeRegistry.load(enabled=[]).names == frozenset()         # [] -> none
    assert EventTypeRegistry.load(enabled=[DIST_NAME]).names == {"x.refplugin.note"}

    # And the config default must be the "absent" state, not the empty one.
    assert ProfilesConfig().enabled is None

    ws = Workspace(tmp_path / "none")
    runtime = Runtime(ws, _config(enabled=[]))
    try:
        assert runtime.types.names == frozenset()
        assert runtime.projections.names == ("supersession_chains", "session_summaries")
    finally:
        runtime.close()


def test_an_orlog_toml_with_no_profiles_table_still_loads_profiles(tmp_path, installed_refplugin):
    """The default has to survive a round trip through orlog.toml --
    save/load must not turn "absent" into "[]" and silently switch every
    existing workspace to loading nothing."""
    from orlog.config import load_config, save_config

    path = tmp_path / "orlog.toml"
    save_config(_config(), path)
    assert load_config(path).profiles.enabled is None
    assert "enabled" not in path.read_text(encoding="utf-8")
