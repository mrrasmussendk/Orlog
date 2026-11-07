"""cli: orlog init/serve/doctor/conformance/replay/inspect/forget (ORLOG-SPEC.md §B1).

Design decisions:

0. `init --client=<name>` and `doctor` are the install surface, and both
   delegate: the per-client config paths and merge strategies live in
   mcp_clients.py, the checks in doctor.py. The spec-mandated commands keep
   this module; what it takes to get orlog running on someone else's
   machine does not belong in the same file as the protocol's CLI.

1. Every subcommand is a plain `(args) -> int` function, independently
   callable and testable without going through argparse or a real process
   -- tests call cmd_init(Namespace(...)) etc. directly, and check return
   codes + printed output, rather than shelling out to `python -m
   orlog.cli`.

2. `orlog serve` acquires the workspace's single-writer lock via
   `with Workspace(...) as ws:` (spec §B1: "concurrent server instances on
   one workspace MUST be prevented") and releases it on exit, including on
   an unhandled exception, since it's a context manager.

3. `orlog serve --http [host]:port` runs the same server over mcp's
   streamable-HTTP transport instead of stdio (spec §B12.5). stdio stays
   the default, and the daemon holds the workspace lock either way -- which
   is the point of the HTTP mode: several MCP clients can share ONE writer
   instead of each spawning its own stdio server and losing the race for
   the lock. The listen address defaults to 127.0.0.1; this transport has
   no authentication of its own, so binding it to anything else is an
   explicit decision the operator has to type out.

4. `orlog conformance` shells out to pytest against this package's own
   tests/conformance/ directory -- the suite "ships with the reference
   implementation" (spec §6) and --target=self runs it against THIS
   installation. A real --target=<endpoint> (a remote server) is out of
   scope: spec's MCP transport is stdio-only in v1.0, so there is no
   network recall endpoint to point a remote target at yet.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from orlog.config import OrlogConfig, WorkspaceConfig, load_config, save_config
from orlog.errors import LockedError
from orlog.mcp_clients import (
    CLIENTS,
    DEFAULT_SERVER_NAME,
    ClientConfigError,
    config_path_for,
    register,
)
from orlog.models.event import EventDraft
from orlog.vault import VAULT_KEY_ENV, Vault, generate_key
from orlog.workspace import Workspace


def _default_claude_code_config_path() -> Path:
    return config_path_for(CLIENTS["claude-code"])


def _client_config_path(client_key: str) -> Path:
    """Where `init --client=<key>` will write.

    claude-code resolves through `_default_claude_code_config_path` rather
    than going straight to mcp_clients: that function is the seam the test
    suite monkeypatches to keep `cmd_init` away from the real
    ~/.claude.json, and a refactor that quietly bypassed it would have
    tests editing the developer's own Claude Code config.
    """
    if client_key == "claude-code":
        return _default_claude_code_config_path()
    return config_path_for(CLIENTS[client_key])


def write_claude_code_mcp_config(
    ws: Workspace, vault_key: str, *, server_name: str = "orlog", config_path: Path | None = None
) -> Path:
    """Register this workspace as an MCP server in Claude Code's config.

    Kept as a named function (rather than folded into `register`) because
    `--claude-code` is the flag the README, the Homebrew formula's caveats
    and every existing install instruction name, and this is the one client
    whose config path tests monkeypatch via
    `_default_claude_code_config_path`.
    """
    return register(
        "claude-code",
        ws,
        vault_key,
        server_name=server_name,
        config_path=config_path or _default_claude_code_config_path(),
    )


def _requested_clients(args: argparse.Namespace) -> list[str]:
    """The MCP clients to register, from --client (repeatable) plus the
    older --claude-code flag, de-duplicated and order-preserving."""
    requested = list(getattr(args, "client", None) or [])
    if getattr(args, "claude_code", False) and "claude-code" not in requested:
        requested.insert(0, "claude-code")
    seen: dict[str, None] = {}
    for name in requested:
        seen.setdefault(name, None)
    return list(seen)


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.path)
    clients = _requested_clients(args)
    unknown = [c for c in clients if c not in CLIENTS]
    if unknown:
        # argparse's `choices` already rejects a typo'd --client on the
        # command line; this guards the direct `cmd_init(Namespace(...))`
        # call path (design decision 1), and fails before scaffolding so a
        # bad client name leaves no half-set-up workspace behind.
        print(
            f"unknown MCP client(s): {', '.join(unknown)} -- known clients: {', '.join(sorted(CLIENTS))}",
            file=sys.stderr,
        )
        return 2

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
        # other key -- generating a new one here and writing it into a live
        # client config would silently point that client at a key that
        # can't open this vault.
        if clients:
            print(
                "This workspace already existed, so no new vault key was generated. "
                "orlog init doesn't know the original key, and writing a fresh one "
                "into an MCP client's config would break the existing vault -- set "
                f"{VAULT_KEY_ENV} yourself in that server entry's env block."
            )
        return 0

    vault_key = generate_key()
    if not clients:
        print("Set this before running `orlog serve` (a vault key, never written to disk by init):")
        print(f"  {VAULT_KEY_ENV}={vault_key}")
        return 0

    server_name = args.server_name or args.claude_code_name or DEFAULT_SERVER_NAME
    failed = []
    for client_key in clients:
        client = CLIENTS[client_key]
        try:
            config_path = register(
                client_key, ws, vault_key, server_name=server_name, config_path=_client_config_path(client_key)
            )
        except ClientConfigError as exc:
            print(f"registering with {client.label} failed: {exc}", file=sys.stderr)
            failed.append(client_key)
            continue
        print(f"Registered '{server_name}' as an MCP server in {config_path}, including the vault key.")
        if client.note:
            print(f"  {client.label}: {client.note}.")

    if failed:
        # The workspace and its key exist either way, so print the key --
        # without it, a partially-registered workspace is unopenable and the
        # user has no way to finish the job by hand.
        print(
            f"Workspace and vault key were created, but {len(failed)} client(s) were not registered. "
            f"Add this to the server entry's env block yourself:",
            file=sys.stderr,
        )
        print(f"  {VAULT_KEY_ENV}={vault_key}", file=sys.stderr)
        return 2
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Inspect this install and report what would stop it working. Read-only
    (see doctor.py): safe to run against a workspace a live server holds."""
    from orlog.doctor import FAIL, format_checks, run_checks

    checks = run_checks(Path(args.path))
    print(format_checks(checks))
    return 1 if any(c.status == FAIL for c in checks) else 0


def parse_listen(value: str) -> tuple[str, int]:
    """Parse `--http`'s argument: ":8787", "8787" or "0.0.0.0:8787".

    Defaults to 127.0.0.1 because this transport carries no authentication
    (spec §B12.5) -- a workspace reachable from the network because someone
    typed a bare port number is not a default this project is willing to
    ship.
    """
    from orlog.server import DEFAULT_HTTP_PORT

    text = (value or "").strip()
    host, sep, port_text = text.rpartition(":")
    if not sep:
        host, port_text = "", text
    host = host.strip("[]") or "127.0.0.1"
    if not port_text:
        return host, DEFAULT_HTTP_PORT
    try:
        port = int(port_text)
    except ValueError:
        raise ValueError(f"{value!r} is not a listen address -- expected ':8787' or 'host:8787'") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"port {port} is out of range (1-65535)")
    return host, port


def cmd_serve(args: argparse.Namespace) -> int:
    from orlog.errors import RetrieverUnavailableError
    from orlog.runtime import Runtime
    from orlog.server import build_server

    listen = getattr(args, "http", None)
    if listen is not None:
        try:
            host, port = parse_listen(listen)
        except ValueError as exc:
            print(f"{exc}", file=sys.stderr)
            return 2
    else:
        host = port = None

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
            if listen is not None:
                # One daemon, many clients: the workspace lock is held by
                # THIS process for the life of the server, and every
                # connected client writes through it. Over stdio each client
                # spawns its own server and all but the first get E_LOCKED.
                print(f"orlog serving {ws.root} on http://{host}:{port}/mcp", file=sys.stderr)
                build_server(runtime, host=host, port=port).run(transport="streamable-http")
            else:
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
    try:
        import pytest
    except ImportError:
        print("pytest is not installed -- install orlog[dev] to run conformance", file=sys.stderr)
        return 2

    conformance_dir = _conformance_dir()
    if not conformance_dir.is_dir():
        print(f"conformance suite not found at {conformance_dir}", file=sys.stderr)
        return 2

    # In-process, not subprocess: inside a frozen binary sys.executable IS
    # the orlog binary itself, not a real Python interpreter, so
    # `sys.executable -m pytest` (the old approach) cannot work there.
    #
    # --confcutdir: inside the frozen binary's extracted temp dir there is
    # no pyproject.toml for pytest to find while searching upward for a
    # rootdir, so it falls back to conformance_dir itself as rootdir -- which
    # caps conftest.py discovery there too and skips tests/conftest.py (one
    # level up, bundled alongside it), breaking the make_log/make_event
    # fixtures it defines. Explicitly widening confcutdir to the bundle
    # root lets pytest walk up into tests/conftest.py without changing what
    # actually gets collected (still just conformance_dir).
    returncode = int(pytest.main([
        str(conformance_dir), "-v", "--confcutdir", str(conformance_dir.parent.parent),
    ]))
    Path("conformance-report.json").write_text(
        json.dumps({"target": args.target, "passed": returncode == 0}, indent=2)
    )
    print("Conformance report written to conformance-report.json")
    return returncode


def cmd_replay(args: argparse.Namespace) -> int:
    from datetime import datetime as _datetime

    from orlog.projections import ProjectionRegistry, ProjectionStore
    from orlog.storage import EventIndex, SegmentedLog

    ws = Workspace(args.path)
    # Also under the lock: rebuild() truncates and repopulates index.sqlite,
    # which a running server is concurrently reading and writing.
    try:
        with ws:
            log = SegmentedLog(ws.events_dir)

            if not log.verify_chain(full=True):
                print("HASH CHAIN BROKEN", file=sys.stderr)
                return 1
            print(f"hash chain verified ({len(log.read_all())} events)")

            index = EventIndex(ws.index_path)
            index.rebuild(log)

            # §B12.2: `orlog replay` rebuilds EVERY registered projection,
            # core's and each loaded profile's, from the log alone -- G2's
            # "every projection can be rebuilt from the log" is not a
            # property of supersession_chains in particular. The cache is
            # dropped first, so what lands in the projections table is a
            # genuine rebuild and not whatever this process was holding.
            config = load_config(ws.config_path) if ws.config_path.exists() else None
            registry = ProjectionRegistry.load(
                enabled=config.profiles.enabled if config else None,
                disabled=config.profiles.disabled if config else None,
            )
            store = ProjectionStore(registry, index=index)
            built = store.rebuild_all(log.read_all(), built_at=_datetime.now(timezone.utc))
            index.close()
    except LockedError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    print(f"index.sqlite rebuilt at {ws.index_path}")
    for name in registry.names:
        version = built.get(name)
        print(
            f"  projection {name}: "
            + (f"rebuilt from {version.built_from}" if version else "no source events")
        )
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
    from orlog.storage import EventIndex, SegmentedLog

    ws = Workspace(args.path)
    # Under the workspace lock: this command APPENDS (the erasure event),
    # and EventLog caches the chain head in memory at construction. Run
    # unlocked against a live `orlog serve`, this append advanced the log
    # while the server's cached _last_hash went stale, so the server's very
    # next remember() wrote a prev_hash pointing at the wrong event and
    # broke the chain permanently -- with that remember() returning a
    # perfectly normal {event_id} and no error anywhere. spec §B1 requires
    # concurrent access to be prevented by the lockfile; only `serve` was
    # actually taking it.
    try:
        with ws:
            vault = Vault(ws.vault_path)
            try:
                if not vault.forget(args.token):
                    print(f"no such pseudonym: {args.token}", file=sys.stderr)
                    return 1
            finally:
                vault.close()

            log = SegmentedLog(ws.events_dir)
            event = log.append(
                EventDraft(
                    occurred_at=datetime.now(timezone.utc), actor="system", type="x.orlog.forget",
                    payload={"token": args.token},  # what was erased, not the erasure itself, per spec §B7
                )
            )
            # Index the erasure event too. Leaving it out left index.sqlite
            # one event behind the log, which the next `orlog serve` repairs
            # by rebuilding the whole thing -- a full re-index of the
            # workspace as the price of one `orlog forget`.
            index = EventIndex(ws.index_path)
            try:
                index.index_appended(log, event)
            finally:
                index.close()
    except LockedError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    print(f"crypto-shredded {args.token}; recorded the erasure event.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orlog")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="scaffold a new workspace")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--name")
    p.add_argument(
        "--client",
        action="append",
        metavar="NAME",
        choices=sorted(CLIENTS),
        help=(
            "also register this workspace as an MCP server in this client's config, including the "
            "vault key. Repeatable. One of: " + ", ".join(sorted(CLIENTS))
        ),
    )
    p.add_argument(
        "--claude-code",
        action="store_true",
        help="shorthand for --client=claude-code",
    )
    p.add_argument(
        "--server-name",
        default=None,
        help=f"the name to register the server under (default: {DEFAULT_SERVER_NAME!r})",
    )
    # The older spelling, kept working: now that every client is supported,
    # --server-name is the one that reads right -- but --claude-code-name is
    # in released help output and in users' notes.
    p.add_argument("--claude-code-name", default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", help="check this install and report what would stop it working")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("serve", help="run the MCP server (stdio by default)")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument(
        "--http",
        nargs="?",
        const=":8787",
        default=None,
        metavar="[HOST]:PORT",
        help=(
            "serve over streamable HTTP instead of stdio, so several MCP clients can share "
            "one daemon (and one writer lock). Defaults to 127.0.0.1:8787; this transport "
            "has no authentication, so bind it beyond localhost only deliberately."
        ),
    )
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
