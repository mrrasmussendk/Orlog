"""cli: orlog init/serve/conformance/replay/inspect/forget (ORLOG-SPEC.md §B1).

Design decisions:

1. Every subcommand is a plain `(args) -> int` function, independently
   callable and testable without going through argparse or a real process
   -- tests call cmd_init(Namespace(...)) etc. directly, and check return
   codes + printed output, rather than shelling out to `python -m
   orlog.cli`.

2. `orlog serve` acquires the workspace's single-writer lock via
   `with Workspace(...) as ws:` (spec §B1: "concurrent server instances on
   one workspace MUST be prevented") and releases it on exit, including on
   an unhandled exception, since it's a context manager.

3. `orlog conformance` shells out to pytest against this package's own
   tests/conformance/ directory -- the suite "ships with the reference
   implementation" (spec §6) and --target=self runs it against THIS
   installation. A real --target=<endpoint> (a remote server) is out of
   scope: spec's MCP transport is stdio-only in v1.0, so there is no
   network recall endpoint to point a remote target at yet.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from orlog.config import OrlogConfig, WorkspaceConfig, load_config, save_config
from orlog.models.event import EventDraft
from orlog.vault import VAULT_KEY_ENV, Vault, generate_key
from orlog.workspace import Workspace


def _default_claude_code_config_path() -> Path:
    return Path.home() / ".claude.json"


def _orlog_command() -> str:
    """Best-effort path to the currently-running `orlog` executable, for
    embedding as the spawned MCP server's `command`."""
    exe = shutil.which("orlog")
    return str(Path(exe).resolve()) if exe else str(Path(sys.argv[0]).resolve())


def write_claude_code_mcp_config(
    ws: Workspace, vault_key: str, *, server_name: str = "orlog", config_path: Path | None = None
) -> Path:
    """Register this workspace as an MCP server in Claude Code's config.

    Backs up the existing file first: it also holds unrelated Claude Code
    state (session history, prefs), so a failed/partial rewrite must not
    lose that.
    """
    path = config_path or _default_claude_code_config_path()
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} exists but is not valid JSON ({exc}) -- fix or remove it, or add the mcpServers entry yourself") from exc
    else:
        data = {}

    data.setdefault("mcpServers", {})[server_name] = {
        "type": "stdio",
        "command": _orlog_command(),
        "args": ["serve", str(ws.root.resolve())],
        "env": {VAULT_KEY_ENV: vault_key},
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.path)
    ws = Workspace(root)
    ws.scaffold()

    freshly_created = not ws.config_path.exists()
    if freshly_created:
        config = OrlogConfig(workspace=WorkspaceConfig(name=args.name or root.resolve().name))
        save_config(config, ws.config_path)
        print(f"wrote {ws.config_path}")
    else:
        print(f"{ws.config_path} already exists, leaving it alone.")

    print("Workspace initialized.")

    if not freshly_created:
        # A pre-existing workspace already has a vault encrypted under some
        # other key -- generating a new one here and (with --claude-code)
        # writing it into a live config would silently point Claude Code at
        # a key that can't open this vault.
        if args.claude_code:
            print(
                "This workspace already existed, so no new vault key was generated. "
                "orlog init doesn't know the original key, and writing a fresh one "
                "into Claude Code's config would break the existing vault -- set "
                "ORLOG_VAULT_KEY yourself in that mcpServers entry's env block."
            )
        return 0

    vault_key = generate_key()
    if args.claude_code:
        server_name = args.claude_code_name or "orlog"
        try:
            config_path = write_claude_code_mcp_config(ws, vault_key, server_name=server_name)
        except ValueError as exc:
            print(f"Workspace and vault key were created, but registering with Claude Code failed: {exc}", file=sys.stderr)
            print(f"  ORLOG_VAULT_KEY={vault_key}", file=sys.stderr)
            return 2
        print(f"Registered '{server_name}' as an MCP server in {config_path}, including the vault key.")
        print("Restart Claude Code for it to pick up the new server.")
    else:
        print("Set this before running `orlog serve` (a vault key, never written to disk by init):")
        print(f"  ORLOG_VAULT_KEY={vault_key}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from orlog.errors import RetrieverUnavailableError
    from orlog.runtime import Runtime
    from orlog.server import build_server

    ws = Workspace(args.path)
    config = load_config(ws.config_path)
    with ws:
        runtime = Runtime(ws, config)
        try:
            # Warm the embedder here, off the request path -- Runtime.embedder
            # already bounds a cold/network-bound build with a hard timeout
            # (config.retrieval.embedder_build_timeout_s), so this costs at
            # most that long once at startup instead of stalling the first
            # free-text recall() call. Best-effort: a failed/timed-out build
            # here doesn't stop the server from serving exact-key recalls
            # (which never touch the embedder) -- a later free-text recall()
            # just retries lazily and abstains EMBEDDER_UNAVAILABLE per call
            # until it succeeds, same as before this preload existed.
            try:
                runtime.embedder
            except RetrieverUnavailableError as exc:
                print(
                    f"warning: embedder did not become ready at startup ({exc}); "
                    "recall() will retry lazily and abstain (EMBEDDER_UNAVAILABLE) until it succeeds",
                    file=sys.stderr,
                )
            build_server(runtime).run(transport="stdio")
        finally:
            runtime.close()
    return 0


def _conformance_dir() -> Path:
    """Resolve tests/conformance/ both from a source checkout and from
    inside a frozen PyInstaller binary (bundled as `datas` in orlog.spec,
    unpacked under sys._MEIPASS at runtime)."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "tests" / "conformance"
    return Path(__file__).resolve().parents[2] / "tests" / "conformance"


def cmd_conformance(args: argparse.Namespace) -> int:
    import pytest

    conformance_dir = _conformance_dir()
    if not conformance_dir.is_dir():
        print(f"conformance suite not found at {conformance_dir}", file=sys.stderr)
        return 2

    # In-process, not subprocess: inside a frozen binary sys.executable IS
    # the orlog binary itself, not a real Python interpreter, so
    # `sys.executable -m pytest` (the old approach) cannot work there.
    returncode = int(pytest.main([str(conformance_dir), "-v"]))
    Path("conformance-report.json").write_text(
        json.dumps({"target": args.target, "passed": returncode == 0}, indent=2)
    )
    print("Conformance report written to conformance-report.json")
    return returncode


def cmd_replay(args: argparse.Namespace) -> int:
    from orlog.storage import EventIndex, SegmentedLog

    ws = Workspace(args.path)
    log = SegmentedLog(ws.events_dir)

    if not log.verify_chain(full=True):
        print("HASH CHAIN BROKEN", file=sys.stderr)
        return 1
    print(f"hash chain verified ({len(log.read_all())} events)")

    index = EventIndex(ws.index_path)
    index.rebuild(log)
    index.close()
    print(f"index.sqlite rebuilt at {ws.index_path}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    from orlog.storage import SegmentedLog

    ws = Workspace(args.path)
    log = SegmentedLog(ws.events_dir)
    event = log.get(args.event_id)
    if event is None:
        print(f"no such event: {args.event_id}", file=sys.stderr)
        return 1

    print(json.dumps(event.model_dump(mode="json"), indent=2))
    print("\nProvenance (outcome events mentioning this event id):")
    found = False
    for candidate in log.read_all():
        if candidate.type.startswith("outcome.") and args.event_id in json.dumps(candidate.payload):
            found = True
            print(f"  [{candidate.id}] {candidate.type} at {candidate.occurred_at.isoformat()}")
    if not found:
        print("  (none found)")
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    from orlog.storage import SegmentedLog

    ws = Workspace(args.path)
    vault = Vault(ws.vault_path)
    if not vault.forget(args.token):
        print(f"no such pseudonym: {args.token}", file=sys.stderr)
        vault.close()
        return 1
    vault.close()

    log = SegmentedLog(ws.events_dir)
    log.append(
        EventDraft(
            occurred_at=datetime.now(timezone.utc), actor="system", type="x.orlog.forget",
            payload={"token": args.token},  # what was erased, not the erasure itself, per spec §B7
        )
    )
    print(f"crypto-shredded {args.token}; recorded the erasure event.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orlog")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="scaffold a new workspace")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--name")
    p.add_argument(
        "--claude-code",
        action="store_true",
        help="also register this workspace as an MCP server in Claude Code's config (~/.claude.json), including the vault key",
    )
    p.add_argument("--claude-code-name", default=None, help="mcpServers entry name to use (default: 'orlog')")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("serve", help="run the MCP stdio server")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("conformance", help="run the conformance suite")
    p.add_argument("--target", default="self")
    p.set_defaults(func=cmd_conformance)

    p = sub.add_parser("replay", help="rebuild the index, verify the hash chain")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("inspect", help="print an event's provenance chain")
    p.add_argument("event_id")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("forget", help="crypto-shred a pseudonym token")
    p.add_argument("token")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_forget)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
