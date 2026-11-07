"""mcp_clients: register a workspace as an MCP server in the config file of
whichever MCP client the user actually runs.

`orlog init --client=<name>` exists because the install story ends at the
client, not at the binary: an `orlog` on PATH that no agent knows how to
spawn has not been installed in any sense the user cares about. Hand-editing
a JSON config is where that last step goes wrong -- and it is also where the
vault key has to be written, which is the one piece of state orlog never
puts on disk itself.

Design decisions:

1. One registry, two write strategies. Every client here spawns the same
   stdio command with the same args and env; they disagree only on WHERE
   the config lives, what the servers map is called ("mcpServers" vs
   VS Code's "servers"), whether a `type: "stdio"` discriminator is
   expected, and -- for Codex -- whether the file is JSON at all.

2. The config files belong to the user, not to orlog. Every write backs the
   file up first (they hold unrelated state: Claude Code's `~/.claude.json`
   carries session history and preferences), preserves every key it did not
   come to change, and refuses rather than guesses when the existing file
   doesn't parse.

3. Codex's `~/.codex/config.toml` is APPENDED to, not rewritten. Round-
   tripping TOML through a parser would silently drop the user's comments
   and formatting; appending a `[mcp_servers.<name>]` table preserves the
   file byte for byte. The result is re-parsed and rolled back if the
   append didn't produce valid TOML, so a hand-written file we misread
   can't be corrupted.

4. Paths are resolved per-platform at call time, never cached at import --
   tests monkeypatch `Path.home()` and the platform marker, and a frozen
   binary resolves them on whatever machine it runs on.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from orlog.vault import VAULT_KEY_ENV
from orlog.workspace import Workspace

DEFAULT_SERVER_NAME = "orlog"


class ClientConfigError(Exception):
    """A client's config file exists but orlog will not touch it -- it is
    unparseable, or it already has an entry under this server name that a
    rewrite would silently change. The message names what the user should
    do instead."""


@dataclass(frozen=True)
class McpClient:
    key: str
    label: str
    # The map of servers inside the config file. VS Code calls it "servers";
    # everything else follows Claude Desktop's "mcpServers".
    servers_key: str = "mcpServers"
    # Whether to write the `type: "stdio"` discriminator. Harmless where it
    # is optional, required by VS Code, and what Claude Code itself writes.
    include_type: bool = True
    # "json" merges into a JSON object; "toml-append" appends a TOML table.
    format: str = "json"
    # Per-platform config path, relative to $HOME unless noted. `None` for a
    # platform the client does not ship on.
    macos: str | None = None
    linux: str | None = None
    windows: str | None = None
    # Only set for clients whose Windows path is under %APPDATA%.
    windows_appdata: str | None = None
    note: str = ""


CLIENTS: dict[str, McpClient] = {
    "claude-code": McpClient(
        key="claude-code",
        label="Claude Code",
        macos=".claude.json",
        linux=".claude.json",
        windows=".claude.json",
        note="restart Claude Code to pick up the new server",
    ),
    "claude-desktop": McpClient(
        key="claude-desktop",
        label="Claude Desktop",
        include_type=False,
        macos="Library/Application Support/Claude/claude_desktop_config.json",
        linux=".config/Claude/claude_desktop_config.json",
        windows_appdata="Claude/claude_desktop_config.json",
        note="quit and reopen Claude Desktop (closing the window is not enough)",
    ),
    "cursor": McpClient(
        key="cursor",
        label="Cursor",
        include_type=False,
        macos=".cursor/mcp.json",
        linux=".cursor/mcp.json",
        windows=".cursor/mcp.json",
        note="reload Cursor to pick up the new server",
    ),
    "windsurf": McpClient(
        key="windsurf",
        label="Windsurf",
        include_type=False,
        macos=".codeium/windsurf/mcp_config.json",
        linux=".codeium/windsurf/mcp_config.json",
        windows=".codeium/windsurf/mcp_config.json",
        note="reload Windsurf to pick up the new server",
    ),
    "vscode": McpClient(
        key="vscode",
        label="VS Code",
        servers_key="servers",
        macos="Library/Application Support/Code/User/mcp.json",
        linux=".config/Code/User/mcp.json",
        windows_appdata="Code/User/mcp.json",
        note="reload the VS Code window to pick up the new server",
    ),
    "codex": McpClient(
        key="codex",
        label="Codex CLI",
        format="toml-append",
        macos=".codex/config.toml",
        linux=".codex/config.toml",
        windows=".codex/config.toml",
        note="start a new Codex session to pick up the new server",
    ),
}


def orlog_command() -> str:
    """Best-effort absolute path to the currently-running `orlog`
    executable, for embedding as the spawned MCP server's `command`.

    An MCP client spawns this with its own environment, which is NOT the
    shell's -- a bare "orlog" resolves against whatever PATH the client
    inherited from the desktop session, which on macOS in particular does
    not include pipx's or Homebrew's bin directory. An absolute path is the
    difference between a server that starts and one that fails invisibly.
    """
    exe = shutil.which("orlog")
    return str(Path(exe).resolve()) if exe else str(Path(sys.argv[0]).resolve())


def _home() -> Path:
    return Path.home()


def config_path_for(client: McpClient) -> Path:
    """Where this client keeps its MCP server config on this platform."""
    if sys.platform == "darwin":
        relative = client.macos
    elif sys.platform.startswith("win"):
        if client.windows_appdata:
            appdata = os.environ.get("APPDATA")
            base = Path(appdata) if appdata else _home() / "AppData" / "Roaming"
            return base / client.windows_appdata
        relative = client.windows
    else:
        relative = client.linux
    if relative is None:
        raise ClientConfigError(f"{client.label} has no known config location on {sys.platform}")
    return _home() / relative


def server_entry(client: McpClient, ws: Workspace, vault_key: str, *, command: str | None = None) -> dict:
    """The one server definition every client here gets, modulo the
    `type` discriminator."""
    entry: dict[str, object] = {}
    if client.include_type:
        entry["type"] = "stdio"
    entry["command"] = command or orlog_command()
    entry["args"] = ["serve", str(ws.root.resolve())]
    entry["env"] = {VAULT_KEY_ENV: vault_key}
    return entry


def _backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))


def _write_json_config(
    client: McpClient, ws: Workspace, vault_key: str, *, server_name: str, path: Path, command: str | None
) -> Path:
    if path.exists():
        _backup(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise ClientConfigError(
                f"{path} exists but is not valid JSON ({exc}) -- fix or remove it, "
                f"or add the {client.servers_key} entry yourself"
            ) from exc
        if not isinstance(data, dict):
            raise ClientConfigError(f"{path} is valid JSON but not an object -- orlog will not rewrite it")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}

    servers = data.setdefault(client.servers_key, {})
    if not isinstance(servers, dict):
        raise ClientConfigError(f"{path} has a {client.servers_key!r} that is not an object -- orlog will not rewrite it")
    servers[server_name] = server_entry(client, ws, vault_key, command=command)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def _write_toml_config(
    client: McpClient, ws: Workspace, vault_key: str, *, server_name: str, path: Path, command: str | None
) -> Path:
    import tomllib

    import tomli_w

    original = path.read_text(encoding="utf-8") if path.exists() else ""
    if original:
        try:
            existing = tomllib.loads(original)
        except tomllib.TOMLDecodeError as exc:
            raise ClientConfigError(
                f"{path} exists but is not valid TOML ({exc}) -- fix or remove it, "
                f"or add the [mcp_servers.{server_name}] table yourself"
            ) from exc
        if server_name in (existing.get("mcp_servers") or {}):
            raise ClientConfigError(
                f"{path} already has an [mcp_servers.{server_name}] table. orlog appends rather than "
                f"rewrites TOML (so your comments survive), so it will not replace it -- edit that "
                f"table's command/args/env yourself, or pass --server-name to register a second one."
            )

    # The `type` discriminator is a JSON-client convention; Codex infers
    # stdio from the presence of `command`.
    entry = {k: v for k, v in server_entry(client, ws, vault_key, command=command).items() if k != "type"}
    block = tomli_w.dumps({"mcp_servers": {server_name: entry}})

    separator = "" if (not original or original.endswith("\n\n")) else ("\n" if original.endswith("\n") else "\n\n")
    updated = original + separator + block

    _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
    try:
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        # We misread the file (e.g. an inline `mcp_servers = {...}` at the
        # root that our append now duplicates). Put it back exactly as it
        # was rather than leave a config that no longer parses.
        if original:
            path.write_text(original, encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
        raise ClientConfigError(
            f"appending to {path} would have produced invalid TOML ({exc}); the file was left unchanged. "
            f"Add the [mcp_servers.{server_name}] table yourself."
        ) from exc
    return path


def register(
    client_key: str,
    ws: Workspace,
    vault_key: str,
    *,
    server_name: str = DEFAULT_SERVER_NAME,
    config_path: Path | None = None,
    command: str | None = None,
) -> Path:
    """Register `ws` as an MCP server named `server_name` in `client_key`'s
    config, including the vault key. Returns the path written."""
    try:
        client = CLIENTS[client_key]
    except KeyError:
        raise ClientConfigError(
            f"unknown MCP client {client_key!r} -- known clients: {', '.join(sorted(CLIENTS))}"
        ) from None
    path = config_path or config_path_for(client)
    writer = _write_toml_config if client.format == "toml-append" else _write_json_config
    return writer(client, ws, vault_key, server_name=server_name, path=path, command=command)


def registered_servers(client_key: str, *, config_path: Path | None = None) -> dict[str, dict]:
    """Every MCP server this client has registered, for `orlog doctor`.

    Returns {} for a client with no config file at all; raises
    ClientConfigError if the file exists but cannot be read, which is
    itself worth reporting.
    """
    client = CLIENTS[client_key]
    path = config_path or config_path_for(client)
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if client.format == "toml-append":
        import tomllib

        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ClientConfigError(f"{path} is not valid TOML ({exc})") from exc
        servers = data.get("mcp_servers") or {}
    else:
        try:
            data = json.loads(text or "{}")
        except json.JSONDecodeError as exc:
            raise ClientConfigError(f"{path} is not valid JSON ({exc})") from exc
        servers = data.get(client.servers_key) or {} if isinstance(data, dict) else {}
    return servers if isinstance(servers, dict) else {}
