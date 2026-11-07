"""Contract: a profile's whole life, through the real MCP surface
(ORLOG-SPEC.md §B12, §C3).

The other suites test the seams in pieces. This one runs the sequence a
deployment actually goes through -- install a profile, write typed events
through it, restart, replay, then turn the profile OFF -- against a real
`orlog serve --http` daemon in its own process, with the profile installed
as a genuine distribution on that process's path.

It exists because the two bugs this file's assertions pin down were both
invisible to unit tests and immediately obvious end to end:

- `remember(payload=...)` reached Runtime but not the MCP tool in front of
  it, so §B12.1 worked for every caller except the one it was for.
- `[profiles] enabled = []` meant "load everything", so a workspace had no
  way to refuse third-party code at all.

And it pins §C3's MUST, which nothing else tests: disabling a profile must
not make the events it already wrote unreadable. G1 does not depend on
knowing what a payload means.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import anyio
import pytest

from orlog.cli import build_parser, cmd_init
from orlog.config import load_config, save_config
from orlog.vault import VAULT_KEY_ENV, generate_key
from orlog.workspace import Workspace

FIXTURE = Path(__file__).parent / "fixtures" / "orlog_refplugin"
DIST = "orlog-refplugin"
TYPE = "x.refplugin.note"

ENTRY_POINTS = """\
[orlog.event_types]
refplugin = orlog_refplugin:EVENT_TYPES

[orlog.projections]
refplugin = orlog_refplugin:PROJECTIONS

[orlog.tools]
refplugin = orlog_refplugin:TOOLS
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Daemon:
    """A real `orlog serve --http` process, with `site` on its path."""

    def __init__(self, root: Path, env: dict) -> None:
        self.root, self.env, self.port, self.proc = root, env, _free_port(), None

    def __enter__(self) -> "_Daemon":
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "orlog.cli", "serve", str(self.root), "--http", f":{self.port}"],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("daemon exited: " + (self.proc.stderr.read() if self.proc.stderr else ""))
            with socket.socket() as s:
                s.settimeout(0.2)
                if s.connect_ex(("127.0.0.1", self.port)) == 0:
                    return self
            time.sleep(0.05)
        raise TimeoutError("daemon never listened")

    def __exit__(self, *exc) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            self.proc.kill()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def drive(self, body):
        """Open one MCP session and hand `body` (tools, call) -> result."""

        async def run():
            try:
                from mcp.client.streamable_http import streamable_http_client
            except ImportError:  # pragma: no cover -- older SDK name
                from mcp.client.streamable_http import streamablehttp_client as streamable_http_client
            from mcp import ClientSession

            async with streamable_http_client(self.url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = sorted(t.name for t in (await session.list_tools()).tools)

                    async def call(name, args):
                        result = await session.call_tool(name, args)
                        if result.isError:
                            return {"__error__": result.content[0].text}
                        if result.structuredContent:
                            return result.structuredContent.get("result", result.structuredContent)
                        return json.loads(result.content[0].text)

                    return await body(tools, call)

        return anyio.run(run)


@pytest.fixture
def installed_profile(tmp_path):
    """The reference profile, installed as a real distribution, on a path
    the daemon subprocess will also see."""
    site = tmp_path / "site"
    shutil.copytree(FIXTURE, site / "orlog_refplugin")
    dist_info = site / "orlog_refplugin-0.1.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {DIST}\nVersion: 0.1.0\n", encoding="utf-8"
    )
    (dist_info / "entry_points.txt").write_text(ENTRY_POINTS, encoding="utf-8")
    return site


@pytest.fixture
def workspace(tmp_path, installed_profile):
    root = tmp_path / "ws"
    cmd_init(build_parser().parse_args(["init", str(root)]))
    ws = Workspace(root)
    config = load_config(ws.config_path)
    config.retrieval.embedder = "hashing"  # instant, offline
    save_config(config, ws.config_path)

    existing = os.environ.get("PYTHONPATH")
    env = {
        **os.environ,
        VAULT_KEY_ENV: generate_key(),
        "PYTHONPATH": os.pathsep.join([str(installed_profile)] + ([existing] if existing else [])),
    }
    return ws, env


def _run_cli(args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "orlog.cli", *args], env=env, capture_output=True, text=True
    )


def test_a_profiles_whole_life_through_the_mcp_surface(workspace):
    ws, env = workspace

    # -- 1. cold start: the profile's tool is served next to core's -------
    with _Daemon(ws.root, env) as daemon:

        async def write(tools, call):
            assert "notes_about" in tools, tools
            assert {"remember", "recall", "stats"} <= set(tools)
            fact = await call("remember", {
                "text": "Anna moved to Porto in 2024.", "entity": "person:anna",
                "attribute": "city", "value": "Porto",
                "evidence_span": "Anna moved to Porto", "occurred_at": "2024-01-01T00:00:00Z",
            })
            await call("remember", {
                "text": "Anna has since moved to Lisbon.", "entity": "person:anna",
                "attribute": "city", "value": "Lisbon",
                "evidence_span": "moved to Lisbon", "occurred_at": "2026-01-01T00:00:00Z",
            })
            # §B12.1 over MCP, not just from Python -- the gap the e2e found.
            note = await call("remember", {
                "type": TYPE, "payload": {"body": "pin mcp below 2", "topic": "deps"}})
            assert "event_id" in note, note
            served = await call("notes_about", {"topic": "deps"})
            assert [n["body"] for n in served["notes"]] == ["pin mcp below 2"]
            return fact["event_id"], note["event_id"]

        _, note_id = daemon.drive(write)

    # -- 2. restart: the memory is in the log, not the process ------------
    with _Daemon(ws.root, env) as daemon:

        async def read(tools, call):
            now = await call("recall", {"query": "person:anna.city"})
            past = await call("recall", {"query": "person:anna.city", "as_of": "2025-01-01T00:00:00Z"})
            history = await call("recall_history", {"query": "person:anna.city"})
            stats = await call("stats", {})
            return now, past, history, stats

        now, past, history, stats = daemon.drive(read)

    assert now["verified"] and now["claim"].endswith("Lisbon")
    assert past["verified"] and past["claim"].endswith("Porto")
    assert [c["value"] for c in history["chain"]] == ["Porto", "Lisbon"]
    # A fresh process starts its counters at zero: stats describe the
    # process, the log describes the memory.
    assert stats["appends"] == 0 and stats["recalls"] == 2
    assert stats["projections"]["projection_cache_entries"] == 1

    # -- 3. replay rebuilds the profile's projection from the log ---------
    replay = _run_cli(["replay", str(ws.root)], env)
    assert replay.returncode == 0, replay.stderr
    assert "hash chain verified" in replay.stdout
    assert "projection notes_by_topic: rebuilt" in replay.stdout, replay.stdout

    # -- 4. the kill switch -----------------------------------------------
    config = load_config(ws.config_path)
    assert config.profiles.enabled is None, "a fresh workspace must default to loading profiles"
    config.profiles.enabled = []
    save_config(config, ws.config_path)
    assert load_config(ws.config_path).profiles.enabled == [], "[] did not round-trip through orlog.toml"

    with _Daemon(ws.root, env) as daemon:

        async def without_profile(tools, call):
            assert "notes_about" not in tools, tools
            assert {"remember", "recall", "stats"} <= set(tools), tools
            refused = await call("remember", {"type": TYPE, "payload": {"body": "x"}})
            answer = await call("recall", {"query": "person:anna.city"})
            return refused, answer

        refused, answer = daemon.drive(without_profile)

    assert "not a known event type" in refused["__error__"]
    assert answer["verified"], "core memory broke when the profile was turned off"

    # §C3 MUST: an event written by a profile that is no longer loaded is
    # still readable. Nothing else in the suite asserts this.
    inspect = _run_cli(["inspect", note_id, str(ws.root)], env)
    assert inspect.returncode == 0, inspect.stderr
    body = json.loads(inspect.stdout.split("\nProvenance")[0])
    assert body["type"] == TYPE
    assert body["payload"]["body"] == "pin mcp below 2"

    # ...and the chain still verifies, with that projection simply absent.
    replay = _run_cli(["replay", str(ws.root)], env)
    assert replay.returncode == 0, replay.stderr
    assert "hash chain verified" in replay.stdout
    assert "notes_by_topic" not in replay.stdout
