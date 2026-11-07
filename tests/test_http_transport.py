"""Contract: the streamable-HTTP transport (ORLOG-SPEC.md §B12.5).

§B12.5's acceptance criterion is that two MCP client sessions share one
daemon and both remember without lock errors. Over stdio each client spawns
its own server and all but the first get E_LOCKED; over HTTP there is one
daemon, holding one lock, and the clients are sessions on it.

The end-to-end test runs a real `orlog serve --http` subprocess and two
real MCP clients against it. It is slow by this suite's standards (about a
second) but it is the only way to test the thing the criterion is about:
process-level single-writer behaviour.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import anyio
import pytest

from orlog.cli import build_parser, cmd_init, cmd_serve, parse_listen
from orlog.config import load_config, save_config
from orlog.server import DEFAULT_HTTP_PORT
from orlog.vault import VAULT_KEY_ENV, generate_key
from orlog.workspace import Workspace


# -- the listen address ----------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (":8787", ("127.0.0.1", 8787)),
        ("8787", ("127.0.0.1", 8787)),
        ("", ("127.0.0.1", DEFAULT_HTTP_PORT)),
        (":", ("127.0.0.1", DEFAULT_HTTP_PORT)),
        ("0.0.0.0:9000", ("0.0.0.0", 9000)),
        ("[::1]:9000", ("::1", 9000)),
    ],
)
def test_parse_listen(value, expected):
    assert parse_listen(value) == expected


def test_a_bare_port_still_binds_to_localhost():
    # This transport has no authentication. A workspace reachable from the
    # network because someone typed "8787" is not a default worth shipping.
    assert parse_listen("8787")[0] == "127.0.0.1"


@pytest.mark.parametrize("value", ["nope", ":0", ":70000", "host:port"])
def test_a_bad_listen_address_is_refused(value):
    with pytest.raises(ValueError):
        parse_listen(value)


def test_serve_rejects_a_bad_listen_address_before_touching_the_workspace(tmp_path, capsys):
    rc = cmd_serve(build_parser().parse_args(["serve", str(tmp_path), "--http", "nope"]))
    assert rc == 2
    assert "listen address" in capsys.readouterr().err


def test_stdio_is_still_the_default():
    args = build_parser().parse_args(["serve", "."])
    assert args.http is None
    assert build_parser().parse_args(["serve", ".", "--http"]).http == ":8787"


# -- one daemon, two clients -----------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, process: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server exited early: {process.stderr.read() if process.stderr else ''}")
        with socket.socket() as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise TimeoutError(f"server did not start listening on {port}")


@pytest.fixture
def http_daemon(tmp_path):
    root = tmp_path / "shared"
    cmd_init(build_parser().parse_args(["init", str(root)]))
    ws = Workspace(root)
    config = load_config(ws.config_path)
    config.retrieval.embedder = "hashing"  # instant, offline
    save_config(config, ws.config_path)

    port = _free_port()
    env = {**os.environ, VAULT_KEY_ENV: generate_key()}
    process = subprocess.Popen(
        [sys.executable, "-m", "orlog.cli", "serve", str(root), "--http", f":{port}"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_port(port, process)
        yield root, port
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()


async def _remember(url: str, entity: str) -> dict:
    from mcp import ClientSession

    try:  # renamed in mcp 1.30; the old name still works but warns
        from mcp.client.streamable_http import streamable_http_client
    except ImportError:  # pragma: no cover
        from mcp.client.streamable_http import streamablehttp_client as streamable_http_client

    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "remember",
                {
                    "text": f"{entity} is on the pro plan",
                    "entity": entity,
                    "attribute": "plan",
                    "value": "pro",
                },
            )
            assert not result.isError, result.content
            if result.structuredContent:
                # A tool advertised as structured output wraps a plain dict
                # return value under "result"; a dict with declared fields
                # comes back as itself.
                return result.structuredContent.get("result", result.structuredContent)
            return json.loads(result.content[0].text)


def test_two_sessions_share_one_daemon_and_both_remember(http_daemon):
    root, port = http_daemon
    url = f"http://127.0.0.1:{port}/mcp"

    async def both():
        results: list[dict] = []
        async with anyio.create_task_group() as tg:

            async def run(entity: str):
                results.append(await _remember(url, entity))

            tg.start_soon(run, "user:1")
            tg.start_soon(run, "user:2")
        return results

    results = anyio.run(both)

    # Both writes landed -- no E_LOCKED, because there is one writer and the
    # clients are sessions on it.
    assert len(results) == 2
    event_ids = {r["event_id"] for r in results}
    assert len(event_ids) == 2

    from orlog.storage import SegmentedLog

    written = {
        e.payload["entity"] for e in SegmentedLog(Workspace(root).events_dir).read_all() if e.type == "fact"
    }
    assert written == {"user:1", "user:2"}


def test_the_daemon_holds_the_workspace_lock_for_its_whole_life(http_daemon):
    root, _ = http_daemon
    ws = Workspace(root)
    assert ws.lock_path.exists()

    # A second writer -- an `orlog forget`, say -- is refused while the
    # daemon is up, which is §B1's single-writer rule still holding over
    # the new transport.
    from orlog.errors import LockedError

    with pytest.raises(LockedError):
        ws.acquire_lock()
