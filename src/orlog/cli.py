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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from orlog.config import OrlogConfig, WorkspaceConfig, load_config, save_config
from orlog.models.event import EventDraft
from orlog.vault import Vault, generate_key
from orlog.workspace import Workspace


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.path)
    ws = Workspace(root)
    ws.scaffold()

    if ws.config_path.exists():
        print(f"{ws.config_path} already exists, leaving it alone.")
    else:
        config = OrlogConfig(workspace=WorkspaceConfig(name=args.name or root.resolve().name))
        save_config(config, ws.config_path)
        print(f"wrote {ws.config_path}")

    print("Workspace initialized.")
    print("Set this before running `orlog serve` (a vault key, never written to disk by init):")
    print(f"  ORLOG_VAULT_KEY={generate_key()}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from orlog.runtime import Runtime
    from orlog.server import build_server

    ws = Workspace(args.path)
    config = load_config(ws.config_path)
    with ws:
        runtime = Runtime(ws, config)
        try:
            build_server(runtime).run(transport="stdio")
        finally:
            runtime.close()
    return 0


def cmd_conformance(args: argparse.Namespace) -> int:
    conformance_dir = Path(__file__).resolve().parents[2] / "tests" / "conformance"
    if not conformance_dir.is_dir():
        print(f"conformance suite not found at {conformance_dir}", file=sys.stderr)
        return 2

    result = subprocess.run([sys.executable, "-m", "pytest", str(conformance_dir), "-v"])
    Path("conformance-report.json").write_text(
        json.dumps({"target": args.target, "passed": result.returncode == 0}, indent=2)
    )
    print("Conformance report written to conformance-report.json")
    return result.returncode


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
