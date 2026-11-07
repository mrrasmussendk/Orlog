"""doctor: one command that answers "why isn't this working?".

`orlog doctor` exists because the failure modes of an orlog install cluster
into a handful of states that all present identically to the user -- the
agent just has no `remember` tool, or every recall abstains. Each check
below is one of those states, made visible:

* the vault key is missing or malformed, so `serve` cannot open the vault;
* a workspace exists but its `orlog.toml` doesn't parse;
* another process holds the single-writer lock, so this one gets E_LOCKED;
* the configured embedder needs `fastembed` (not installed) or a model
  download (not yet cached), so free-text recall abstains
  EMBEDDER_UNAVAILABLE;
* the configured deriver needs an SDK or an API key that isn't there;
* the MCP client was never registered -- or was, and points at an `orlog`
  executable that has since moved (the standard aftermath of a pipx
  reinstall), which is invisible from inside the client.

Design decisions:

1. Every check is pure inspection. doctor opens no workspace for writing,
   takes no lock, makes no network call and downloads no model -- it must
   be safe to run against a workspace a live server is holding, which is
   exactly when someone reaches for it.

2. Three statuses, and only `fail` is an error. `warn` is for a state that
   is legitimate but worth knowing (no workspace here yet; the embedder
   model will download on first use). Exit code is 1 if anything failed, so
   it is usable in a setup script.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from orlog.vault import VAULT_KEY_ENV

OK = "ok"
WARN = "warn"
FAIL = "fail"

_MARK = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}

MIN_PYTHON = (3, 11)


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _installed(module: str) -> bool:
    """Is this module importable without importing it? `fastembed` pulls in
    onnxruntime, which is seconds and ~100MB of mmap we don't need in order
    to answer "is it there"."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def check_runtime() -> Check:
    version = ".".join(str(n) for n in sys.version_info[:3])
    frozen = getattr(sys, "frozen", False)
    how = "standalone binary" if frozen else f"{sys.executable}"
    if sys.version_info[:2] < MIN_PYTHON:
        return Check(
            "runtime",
            FAIL,
            f"Python {version} is below the {'.'.join(str(n) for n in MIN_PYTHON)} floor orlog requires",
        )
    return Check("runtime", OK, f"Python {version} ({how})")


def check_vault_key() -> Check:
    if not os.environ.get(VAULT_KEY_ENV):
        return Check(
            "vault key",
            WARN,
            f"{VAULT_KEY_ENV} is not set in this shell. `orlog serve` and `orlog forget` need it; "
            f"`orlog init --client=<name>` writes it into the MCP client's config instead, which is "
            f"why an agent can work while your shell cannot",
        )
    # _load_key_from_env is the same decode `Vault` itself does -- reusing it
    # means doctor can never disagree with what serve will accept.
    from orlog.vault import StorageError, _load_key_from_env

    try:
        _load_key_from_env()
    except StorageError as exc:
        return Check("vault key", FAIL, f"{VAULT_KEY_ENV} is set but unusable: {exc}")
    return Check("vault key", OK, f"{VAULT_KEY_ENV} is set and decodes to a 32-byte key")


def check_workspace(path: Path) -> tuple[Check, object | None]:
    """Returns the check plus the loaded config (or None), so later checks
    can report on what this workspace actually configures."""
    from orlog.config import load_config
    from orlog.workspace import Workspace

    ws = Workspace(path)
    if not ws.config_path.exists():
        return (
            Check(
                "workspace",
                WARN,
                f"no orlog.toml in {path} -- run `orlog init {path}` to scaffold one",
            ),
            None,
        )
    try:
        config = load_config(ws.config_path)
    except Exception as exc:  # pydantic validation, TOML syntax, unknown keys
        return Check("workspace", FAIL, f"{ws.config_path} does not load: {exc}"), None
    events = ws.events_dir
    segments = len(list(events.glob("*.jsonl"))) if events.exists() else 0
    return (
        Check(
            "workspace",
            OK,
            f"{ws.root.resolve()} (name={config.workspace.name!r}, {segments} log segment(s), "
            f"index {'present' if ws.index_path.exists() else 'not built yet'})",
        ),
        config,
    )


def check_lock(path: Path) -> Check:
    from orlog.workspace import Workspace, _pid_alive

    ws = Workspace(path)
    lock = ws.lock_path
    if not lock.exists():
        return Check("writer lock", OK, "free")
    try:
        pid = int(lock.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return Check("writer lock", WARN, f"{lock} is unreadable; orlog will reclaim it as stale")
    if _pid_alive(pid):
        return Check(
            "writer lock",
            WARN,
            f"held by pid {pid} (a server is running). A second `orlog serve` on this workspace "
            f"will fail with E_LOCKED -- use `orlog serve --http` to share one writer",
        )
    return Check("writer lock", OK, f"a dead pid ({pid}) left it behind; orlog will reclaim it")


def check_embedder(config: object | None) -> Check:
    from orlog.retrieval_hybrid import _default_fastembed_cache_dir

    name = config.retrieval.embedder if config is not None else "BAAI/bge-small-en-v1.5"
    if name == "hashing":
        return Check("embedder", OK, "hashing (dependency-free, offline; not real semantic similarity)")
    if not _installed("fastembed"):
        return Check(
            "embedder",
            FAIL,
            f"{name} needs the `fastembed` package, which is not installed -- `pip install fastembed`, "
            f"or set [retrieval] embedder = \"hashing\" in orlog.toml for the dependency-free stand-in. "
            f"Until then, free-text recall abstains EMBEDDER_UNAVAILABLE (exact entity.attribute "
            f"lookups are unaffected)",
        )
    cache = _default_fastembed_cache_dir()
    cached = cache.exists() and any(cache.iterdir())
    if not cached:
        return Check(
            "embedder",
            WARN,
            f"{name} is installed but its model is not cached in {cache} -- the first free-text "
            f"recall downloads it (and abstains EMBEDDER_UNAVAILABLE if that exceeds "
            f"[retrieval] embedder_build_timeout_s)",
        )
    return Check("embedder", OK, f"{name}, model cached in {cache}")


def check_deriver(config: object | None) -> Check:
    if config is None:
        return Check("deriver", OK, "scripted (the default: deterministic, no API key)")
    backend = config.deriver.backend
    if backend == "scripted":
        return Check("deriver", OK, "scripted (deterministic, no API key)")
    module = {"anthropic": "anthropic", "openai": "openai"}.get(backend)
    if module is None:
        return Check("deriver", WARN, f"unrecognized backend {backend!r}; orlog will fail at startup if it cannot build it")
    key_env = config.deriver.api_key_env
    problems = []
    if not _installed(module):
        problems.append(f"the `{module}` package is not installed (pip install 'orlog[{module}]')")
    if not os.environ.get(key_env):
        problems.append(f"{key_env} is not set")
    if problems:
        return Check("deriver", FAIL, f"backend={backend!r} but " + "; ".join(problems))
    return Check("deriver", OK, f"{backend} ({config.deriver.model}), {key_env} is set")


def _is_orlog_server(name: str, entry: dict) -> bool:
    """Is this one of ours?

    Matched on the server name OR the command's own name, because either can
    be the only evidence: `--server-name memory` renames the entry, and a
    client config written by hand (or by a wrapper script) can spawn orlog
    under some other command name. Over-matching here costs a line of
    report; under-matching means doctor silently says "no orlog server" to
    someone staring at one.
    """
    if "orlog" in name.lower():
        return True
    command = str(entry.get("command", ""))
    return bool(command) and Path(command).name.lower().startswith("orlog")


def check_mcp_clients() -> list[Check]:
    """One check per MCP client that has a config file, reporting whether an
    orlog server is registered there and whether its command still exists."""
    from orlog.mcp_clients import CLIENTS, ClientConfigError, config_path_for, registered_servers

    checks: list[Check] = []
    for key in sorted(CLIENTS):
        client = CLIENTS[key]
        try:
            path = config_path_for(client)
        except ClientConfigError:
            continue
        if not path.exists():
            continue
        try:
            servers = registered_servers(key, config_path=path)
        except ClientConfigError as exc:
            checks.append(Check(f"mcp: {key}", FAIL, str(exc)))
            continue
        orlog_servers = {
            name: entry for name, entry in servers.items() if isinstance(entry, dict) and _is_orlog_server(name, entry)
        }
        if not orlog_servers:
            checks.append(
                Check(
                    f"mcp: {key}",
                    WARN,
                    f"{client.label} is configured ({path}) but has no orlog server -- "
                    f"`orlog init <path> --client={key}` registers one",
                )
            )
            continue
        for name, entry in sorted(orlog_servers.items()):
            command = str(entry.get("command", ""))
            args = entry.get("args") or []
            workspace = next((a for a in args if a not in ("serve",)), "?")
            env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
            details = f"{client.label}: {name!r} -> {command} (workspace {workspace})"
            if command and not Path(command).exists():
                checks.append(
                    Check(
                        f"mcp: {key}",
                        FAIL,
                        f"{details} -- that command no longer exists. Re-run "
                        f"`orlog init {workspace} --client={key}` to point it at the current orlog",
                    )
                )
            elif VAULT_KEY_ENV not in env:
                checks.append(
                    Check(
                        f"mcp: {key}",
                        FAIL,
                        f"{details} -- its env has no {VAULT_KEY_ENV}, so the server cannot open the vault",
                    )
                )
            else:
                checks.append(Check(f"mcp: {key}", OK, details))
    if not checks:
        checks.append(
            Check(
                "mcp clients",
                WARN,
                "no MCP client config found. Register one with "
                "`orlog init <path> --client=claude-code` (or --client=claude-desktop|cursor|vscode|windsurf|codex)",
            )
        )
    return checks


def run_checks(path: Path) -> list[Check]:
    checks = [check_runtime(), check_vault_key()]
    workspace_check, config = check_workspace(path)
    checks.append(workspace_check)
    if config is not None:
        checks.append(check_lock(path))
    checks.append(check_embedder(config))
    checks.append(check_deriver(config))
    checks.extend(check_mcp_clients())
    return checks


def format_checks(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = [f"[{_MARK[c.status]}] {c.name.ljust(width)}  {c.detail}" for c in checks]
    failures = [c for c in checks if c.status == FAIL]
    warnings = [c for c in checks if c.status == WARN]
    lines.append("")
    if failures:
        lines.append(f"{len(failures)} problem(s) to fix" + (f", {len(warnings)} warning(s)" if warnings else ""))
    elif warnings:
        lines.append(f"No problems. {len(warnings)} warning(s) -- read them if something is not working.")
    else:
        lines.append("Everything checks out.")
    return "\n".join(lines)
