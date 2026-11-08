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
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from orlog.vault import VAULT_KEY_ENV
from orlog.workspace import Workspace

DEFAULT_SERVER_NAME = "orlog"


class ClientConfigError(Exception):
    """A client's config file exists but orlog will not write what was
    asked -- it is unparseable, or the write would destroy something: an
    entry under this server name belonging to a DIFFERENT workspace, whose
    env block holds the only copy of that workspace's vault key. (Both
    writers refuse that. The TOML writer additionally refuses to replace
    this workspace's own entry, because it appends rather than rewrites;
    the message says which field to change by hand.) Every message names
    what to do instead."""


@dataclass(frozen=True)
class RegisteredServer:
    """One server entry found in a client's config, and which scope it was
    found in ("global", or "project <path>")."""

    scope: str
    name: str
    entry: dict


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
    # Whether this client ALSO keeps per-directory server maps that orlog
    # should look in when reporting (Claude Code's projects.<path>.mcpServers).
    project_scoped: bool = False
    note: str = ""


CLIENTS: dict[str, McpClient] = {
    "claude-code": McpClient(
        key="claude-code",
        label="Claude Code",
        macos=".claude.json",
        linux=".claude.json",
        windows=".claude.json",
        project_scoped=True,
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
    """Absolute path to the `orlog` that is running THIS command, for
    embedding as the spawned MCP server's `command`.

    An MCP client spawns this with its own environment, which is NOT the
    shell's -- a bare "orlog" resolves against whatever PATH the client
    inherited from the desktop session, which on macOS in particular does
    not include pipx's or Homebrew's bin directory. An absolute path is the
    difference between a server that starts and one that fails invisibly.

    PATH is consulted LAST, not first. A machine with more than one orlog
    (a pipx install plus a checkout's editable install, say) resolves
    `shutil.which("orlog")` to whichever comes first on PATH -- which is
    not necessarily the one the user just invoked, and registering the
    other one points the agent at a different install's code. Worse, it
    fails silently: the path exists, so `orlog doctor` calls it fine.
    So: the running argv[0] if it is an orlog, then the console script
    installed beside this interpreter (what pip/pipx/venv creates), and
    only then PATH.

    Symlinks are made absolute but NOT resolved. Homebrew's
    /opt/homebrew/bin/orlog is a symlink into
    /opt/homebrew/Cellar/orlog/<version>/bin/orlog, and resolving it
    records a path with the version number in it -- which `brew upgrade`
    deletes, breaking the registered server for the one install path the
    formula exists to serve. pipx's ~/.local/bin/orlog is the same shape.
    The symlink is the stable name; follow it at spawn time, not now.
    """
    argv0 = Path(os.path.abspath(sys.argv[0]))
    if argv0.name.startswith("orlog") and argv0.exists():
        # A console script or the frozen binary, invoked directly.
        return str(argv0)

    # `python -m orlog.cli`: argv[0] is cli.py, but the console script for
    # THIS interpreter sits next to it.
    script = "orlog.exe" if sys.platform.startswith("win") else "orlog"
    sibling = Path(os.path.abspath(sys.executable)).parent / script
    if sibling.exists():
        return str(sibling)

    exe = shutil.which("orlog")
    return str(Path(os.path.abspath(exe))) if exe else str(argv0)


def ephemeral_command_reason(command: str) -> str | None:
    """Why this command path will not survive, or None if it looks durable.

    `uvx orlog ...` runs from uv's content-addressed tool cache, so the
    path it would register (~/.cache/uv/archive-v0/<hash>/bin/orlog)
    changes with the version and disappears on `uv cache clean` -- a
    registration that works until it silently doesn't. The same goes for
    anything run out of a temp directory. uvx is a fine way to try orlog;
    it is not a way to install the server an agent spawns for months.
    """
    if not command:
        return None
    path = Path(command)
    parts = set(path.parts)
    cache_dir = os.environ.get("UV_CACHE_DIR")
    in_uv_cache = "archive-v0" in parts or ("uv" in parts and ".cache" in parts)
    if cache_dir and str(path).startswith(str(Path(os.path.abspath(cache_dir)))):
        in_uv_cache = True
    if in_uv_cache:
        return (
            "it lives in uv's tool cache, which is keyed by version and cleared by "
            "`uv cache clean` -- install orlog (pipx install orlog, or brew) before registering, "
            "so the path survives"
        )
    tmp = Path(os.path.abspath(tempfile.gettempdir()))
    try:
        path.relative_to(tmp)
    except ValueError:
        return None
    return f"it lives under the temporary directory {tmp}, which is not a durable install location"


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


def entry_workspace(entry: dict) -> Path | None:
    """The workspace an existing server entry points at, from its args."""
    args = entry.get("args") if isinstance(entry, dict) else None
    if not isinstance(args, list):
        return None
    for arg in args:
        if isinstance(arg, str) and arg != "serve":
            return Path(arg)
    return None


def _same_workspace(entry: dict, ws: Workspace) -> bool:
    existing = entry_workspace(entry)
    if existing is None:
        return False
    return existing.resolve() == ws.root.resolve()


def _refuse_to_clobber_another_workspace(
    existing: object, ws: Workspace, *, server_name: str, path: Path
) -> None:
    """Replacing an entry that points at a DIFFERENT workspace destroys that
    workspace's vault key.

    That env block is the only copy on disk -- orlog deliberately never
    writes the key anywhere else -- so overwriting it makes the other
    workspace's encrypted pseudonym registry unrecoverable. Re-registering
    the SAME workspace is a repair and is allowed.
    """
    if not isinstance(existing, dict) or _same_workspace(existing, ws):
        return
    other = entry_workspace(existing)
    where = f" (it points at {other})" if other else ""
    raise ClientConfigError(
        f"{path} already has a server named {server_name!r} for a different workspace{where}. "
        f"Replacing it would destroy that workspace's only copy of its {VAULT_KEY_ENV}, making its "
        f"vault unrecoverable -- pass --server-name to register this one alongside it."
    )


def find_registered_vault_key(
    ws: Workspace, *, path_for: Callable[[str], Path] | None = None
) -> tuple[str, str] | None:
    """The vault key already recorded for `ws` in some client's config, with
    a label saying where it came from.

    This is what makes repairing an existing workspace possible. `orlog
    init` refuses to mint a new key for a workspace that already has a
    vault (a fresh key cannot open it), so a re-registration has to recover
    the real key -- and a stale entry still carries it; it is the command
    path that went bad, not the key.
    """
    resolve = path_for or (lambda key: config_path_for(CLIENTS[key]))
    for key in sorted(CLIENTS):
        try:
            path = resolve(key)
            for server in iter_registered_servers(key, config_path=path):
                if not _same_workspace(server.entry, ws):
                    continue
                env = server.entry.get("env")
                if isinstance(env, dict) and env.get(VAULT_KEY_ENV):
                    return str(env[VAULT_KEY_ENV]), f"{CLIENTS[key].label} ({path})"
        except ClientConfigError:
            continue  # an unreadable config is doctor's business, not this lookup's
    return None


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
    _refuse_to_clobber_another_workspace(servers.get(server_name), ws, server_name=server_name, path=path)
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
        previous = (existing.get("mcp_servers") or {}).get(server_name)
        if isinstance(previous, dict):
            # orlog appends rather than rewrites TOML so the user's comments
            # and formatting survive, which means it cannot replace a table
            # in place. Say which case this is: repairing this workspace's
            # own entry needs one field changed, while another workspace's
            # entry must not be touched at all (its env block is the only
            # copy of that vault key).
            if _same_workspace(previous, ws):
                raise ClientConfigError(
                    f"{path} already has an [mcp_servers.{server_name}] table for this workspace. "
                    f"orlog appends rather than rewrites TOML, so your comments survive -- set that "
                    f"table's command to {command or orlog_command()} yourself."
                )
            other = entry_workspace(previous)
            where = f" (it points at {other})" if other else ""
            raise ClientConfigError(
                f"{path} already has an [mcp_servers.{server_name}] table for a different "
                f"workspace{where}. Replacing it would destroy that workspace's only copy of its "
                f"{VAULT_KEY_ENV} -- pass --server-name to register this one alongside it."
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


def _load_client_config(client: McpClient, path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if client.format == "toml-append":
        import tomllib

        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ClientConfigError(f"{path} is not valid TOML ({exc})") from exc
    try:
        data = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise ClientConfigError(f"{path} is not valid JSON ({exc})") from exc
    return data if isinstance(data, dict) else {}


def registered_servers(client_key: str, *, config_path: Path | None = None) -> dict[str, dict]:
    """The servers this client registers GLOBALLY (its top-level map).

    Returns {} for a client with no config file at all; raises
    ClientConfigError if the file exists but cannot be read, which is
    itself worth reporting. Use `iter_registered_servers` to also see
    project-scoped registrations.
    """
    client = CLIENTS[client_key]
    path = config_path or config_path_for(client)
    if not path.exists():
        return {}
    data = _load_client_config(client, path)
    key = "mcp_servers" if client.format == "toml-append" else client.servers_key
    servers = data.get(key) or {}
    return servers if isinstance(servers, dict) else {}


def iter_registered_servers(client_key: str, *, config_path: Path | None = None) -> list[RegisteredServer]:
    """Every server this client registers, in every scope it supports.

    Claude Code keeps a second, per-directory map at
    `projects.<path>.mcpServers` -- that is where `claude mcp add` without
    a global scope puts a server. Reading only the top-level map reports a
    perfectly good project-scoped registration as absent, which is the
    opposite of what doctor is for.
    """
    client = CLIENTS[client_key]
    path = config_path or config_path_for(client)
    if not path.exists():
        return []
    found = [
        RegisteredServer("global", name, entry)
        for name, entry in registered_servers(client_key, config_path=path).items()
        if isinstance(entry, dict)
    ]
    if not client.project_scoped:
        return found
    projects = _load_client_config(client, path).get("projects")
    if isinstance(projects, dict):
        for project_path, project in sorted(projects.items()):
            servers = project.get(client.servers_key) if isinstance(project, dict) else None
            if not isinstance(servers, dict):
                continue
            found += [
                RegisteredServer(f"project {project_path}", name, entry)
                for name, entry in servers.items()
                if isinstance(entry, dict)
            ]
    return found
