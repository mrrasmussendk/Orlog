"""Contract: each CLI subcommand (except serve/conformance, which spawn a
process or a subprocess) works end to end against a real scratch
workspace -- ORLOG-SPEC.md §B1.
"""

from datetime import datetime, timezone
import sys

import pytest

import json

from orlog.cli import build_parser, cmd_conformance, cmd_forget, cmd_init, cmd_inspect, cmd_replay, cmd_serve, write_claude_code_mcp_config
from orlog.config import load_config, save_config
from orlog.models.event import EventDraft
from orlog.vault import Vault, generate_key
from orlog.workspace import Workspace

T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    monkeypatch.setenv("ORLOG_VAULT_KEY", generate_key())


def test_parser_wires_every_subcommand():
    parser = build_parser()
    for args in (["init"], ["serve"], ["conformance"], ["replay"], ["inspect", "ev-1"], ["forget", "TOKEN_1"]):
        parsed = parser.parse_args(args)
        assert callable(parsed.func)


def test_init_scaffolds_a_workspace_and_writes_config(tmp_path, capsys):
    root = tmp_path / "myproject"
    args = build_parser().parse_args(["init", str(root), "--name", "myproject"])

    rc = cmd_init(args)

    assert rc == 0
    ws = Workspace(root)
    assert ws.config_path.exists()
    assert ws.events_dir.is_dir()
    config = load_config(ws.config_path)
    assert config.workspace.name == "myproject"
    assert "ORLOG_VAULT_KEY=" in capsys.readouterr().out


def test_init_is_idempotent_and_does_not_overwrite_existing_config(tmp_path):
    root = tmp_path / "myproject"
    args = build_parser().parse_args(["init", str(root)])
    cmd_init(args)
    ws = Workspace(root)
    original = ws.config_path.read_text(encoding="utf-8")

    cmd_init(args)  # second run

    assert ws.config_path.read_text(encoding="utf-8") == original


def test_write_claude_code_mcp_config_creates_a_new_file(tmp_path):
    root = tmp_path / "myproject"
    cmd_init(build_parser().parse_args(["init", str(root)]))
    ws = Workspace(root)
    claude_config = tmp_path / "claude.json"

    result_path = write_claude_code_mcp_config(ws, "the-vault-key", config_path=claude_config)

    assert result_path == claude_config
    data = json.loads(claude_config.read_text(encoding="utf-8"))
    entry = data["mcpServers"]["orlog"]
    assert entry["args"] == ["serve", str(root.resolve())]
    assert entry["env"] == {"ORLOG_VAULT_KEY": "the-vault-key"}


def test_write_claude_code_mcp_config_preserves_unrelated_data_and_backs_up(tmp_path):
    root = tmp_path / "myproject"
    cmd_init(build_parser().parse_args(["init", str(root)]))
    ws = Workspace(root)
    claude_config = tmp_path / "claude.json"
    claude_config.write_text(json.dumps({"someOtherSetting": True, "mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")

    write_claude_code_mcp_config(ws, "the-vault-key", config_path=claude_config)

    data = json.loads(claude_config.read_text(encoding="utf-8"))
    assert data["someOtherSetting"] is True
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert "orlog" in data["mcpServers"]
    assert claude_config.with_suffix(".json.bak").exists()


def test_init_with_claude_code_registers_an_mcp_server(tmp_path, monkeypatch):
    claude_config = tmp_path / "claude.json"
    monkeypatch.setattr("orlog.cli._default_claude_code_config_path", lambda: claude_config)
    root = tmp_path / "myproject"

    rc = cmd_init(build_parser().parse_args(["init", str(root), "--claude-code"]))

    assert rc == 0
    data = json.loads(claude_config.read_text(encoding="utf-8"))
    assert "ORLOG_VAULT_KEY" in data["mcpServers"]["orlog"]["env"]


def test_init_with_claude_code_reports_a_clear_error_on_malformed_existing_json(tmp_path, monkeypatch, capsys):
    claude_config = tmp_path / "claude.json"
    claude_config.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr("orlog.cli._default_claude_code_config_path", lambda: claude_config)
    root = tmp_path / "myproject"

    rc = cmd_init(build_parser().parse_args(["init", str(root), "--claude-code"]))

    assert rc == 2
    err = capsys.readouterr().err
    assert "not valid JSON" in err
    assert "ORLOG_VAULT_KEY=" in err  # the key is still surfaced, not lost
    # The workspace itself must still be usable even though registration failed.
    assert Workspace(root).config_path.exists()


def test_init_with_claude_code_on_an_existing_workspace_does_not_clobber_the_key(tmp_path, monkeypatch):
    claude_config = tmp_path / "claude.json"
    monkeypatch.setattr("orlog.cli._default_claude_code_config_path", lambda: claude_config)
    root = tmp_path / "myproject"
    cmd_init(build_parser().parse_args(["init", str(root), "--claude-code"]))
    original = claude_config.read_text(encoding="utf-8")

    rc = cmd_init(build_parser().parse_args(["init", str(root), "--claude-code"]))  # second run

    assert rc == 0
    assert claude_config.read_text(encoding="utf-8") == original


def test_replay_verifies_the_chain_and_rebuilds_the_index(tmp_path):
    from orlog.storage import SegmentedLog

    root = tmp_path / "myproject"
    cmd_init(build_parser().parse_args(["init", str(root)]))
    ws = Workspace(root)
    log = SegmentedLog(ws.events_dir)
    log.append(EventDraft(occurred_at=T1, actor="test", type="fact", payload={"entity": "user:1", "attribute": "plan", "value": "pro"}))

    rc = cmd_replay(build_parser().parse_args(["replay", str(root)]))

    assert rc == 0
    assert ws.index_path.exists()


def test_replay_reports_failure_when_the_chain_is_broken(tmp_path, capsys):
    from orlog.storage import SegmentedLog

    root = tmp_path / "myproject"
    cmd_init(build_parser().parse_args(["init", str(root)]))
    ws = Workspace(root)
    log = SegmentedLog(ws.events_dir)
    # A lone tampered event is undetectable by hash alone (nothing else
    # references its hash) -- a second event's stored prev_hash is what
    # makes tampering with the first one detectable.
    log.append(EventDraft(occurred_at=T1, actor="test", type="fact", payload={"entity": "user:1", "attribute": "plan", "value": "pro"}))
    log.append(EventDraft(occurred_at=T1, actor="test", type="fact", payload={"entity": "user:2", "attribute": "plan", "value": "free"}))

    segment = ws.events_dir / "log-00001.jsonl"
    tampered = segment.read_text(encoding="utf-8").replace('"pro"', '"enterprise"')
    segment.write_text(tampered, encoding="utf-8")

    rc = cmd_replay(build_parser().parse_args(["replay", str(root)]))

    assert rc == 1
    assert "BROKEN" in capsys.readouterr().err


def test_inspect_prints_the_event_and_reports_no_provenance_when_none_exists(tmp_path, capsys):
    from orlog.storage import SegmentedLog

    root = tmp_path / "myproject"
    cmd_init(build_parser().parse_args(["init", str(root)]))
    ws = Workspace(root)
    log = SegmentedLog(ws.events_dir)
    event = log.append(EventDraft(occurred_at=T1, actor="test", type="fact", payload={"entity": "user:1", "attribute": "plan", "value": "pro"}))

    rc = cmd_inspect(build_parser().parse_args(["inspect", event.id, str(root)]))

    out = capsys.readouterr().out
    assert rc == 0
    assert event.id in out
    assert "(none found)" in out


def test_inspect_of_an_unknown_event_id_fails(tmp_path):
    root = tmp_path / "myproject"
    cmd_init(build_parser().parse_args(["init", str(root)]))

    rc = cmd_inspect(build_parser().parse_args(["inspect", "nonexistent", str(root)]))

    assert rc == 1


def test_forget_crypto_shreds_and_appends_an_erasure_event(tmp_path):
    from orlog.storage import SegmentedLog

    root = tmp_path / "myproject"
    cmd_init(build_parser().parse_args(["init", str(root)]))
    ws = Workspace(root)
    vault = Vault(ws.vault_path)
    token = vault.tokenize("alice@example.com", kind="EMAIL")
    vault.close()

    rc = cmd_forget(build_parser().parse_args(["forget", token, str(root)]))

    assert rc == 0
    vault = Vault(ws.vault_path)
    assert vault.resolve(token) is None  # crypto-shredded
    vault.close()

    log = SegmentedLog(ws.events_dir)
    forget_events = [e for e in log.read_all() if e.type == "x.orlog.forget"]
    assert len(forget_events) == 1
    assert forget_events[0].payload["token"] == token


def test_forget_of_an_unknown_token_fails(tmp_path):
    root = tmp_path / "myproject"
    cmd_init(build_parser().parse_args(["init", str(root)]))

    rc = cmd_forget(build_parser().parse_args(["forget", "EMAIL_999", str(root)]))

    assert rc == 1


class _StubMcpServer:
    """Stands in for FastMCP -- .run() would otherwise block forever reading
    stdio, which a unit test can't do."""

    def __init__(self):
        self.ran = False

    def run(self, transport):
        self.ran = True


def test_serve_preloads_the_embedder_before_running(tmp_path, monkeypatch):
    # Runtime.embedder used to be built lazily on the first free-text
    # recall() call -- a cold model load/download on the request path. It
    # must now be warmed during cmd_serve, before the server ever starts
    # accepting calls.
    root = tmp_path / "myproject"
    cmd_init(build_parser().parse_args(["init", str(root)]))
    ws = Workspace(root)
    config = load_config(ws.config_path)
    config.retrieval.embedder = "hashing"  # instant, dependency-free build
    save_config(config, ws.config_path)

    captured = {}

    def fake_build_server(runtime):
        captured["runtime"] = runtime
        return _StubMcpServer()

    monkeypatch.setattr("orlog.server.build_server", fake_build_server)

    rc = cmd_serve(build_parser().parse_args(["serve", str(root)]))

    assert rc == 0
    assert captured["runtime"]._embedder is not None
    assert captured["runtime"].embedder.name  # actually usable, not just non-None


def test_serve_keeps_running_when_the_embedder_preload_times_out(tmp_path, monkeypatch, capsys):
    from orlog.errors import RetrieverUnavailableError

    root = tmp_path / "myproject"
    cmd_init(build_parser().parse_args(["init", str(root)]))

    def raise_unavailable(self):
        raise RetrieverUnavailableError("embedder did not become ready")

    monkeypatch.setattr("orlog.runtime.Runtime.embedder", property(raise_unavailable))
    monkeypatch.setattr("orlog.server.build_server", lambda runtime: _StubMcpServer())

    rc = cmd_serve(build_parser().parse_args(["serve", str(root)]))

    assert rc == 0
    assert "embedder" in capsys.readouterr().err.lower()


def test_conformance_dir_resolves_inside_meipass_when_frozen(tmp_path, monkeypatch):
    from orlog.cli import _conformance_dir

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert _conformance_dir() == tmp_path / "tests" / "conformance"


def test_conformance_dir_resolves_the_source_tree_when_not_frozen(monkeypatch):
    from orlog.cli import _conformance_dir

    monkeypatch.delattr(sys, "frozen", raising=False)

    result = _conformance_dir()

    assert result.name == "conformance" and result.parent.name == "tests"


def test_conformance_runs_pytest_in_process_and_writes_a_report(tmp_path, monkeypatch):
    from orlog import cli as cli_module

    conformance_dir = tmp_path / "conformance"
    conformance_dir.mkdir()
    monkeypatch.setattr(cli_module, "_conformance_dir", lambda: conformance_dir)
    monkeypatch.chdir(tmp_path)

    captured = {}

    def fake_main(pytest_args):
        captured["args"] = pytest_args
        return 0

    monkeypatch.setattr(pytest, "main", fake_main)

    rc = cli_module.cmd_conformance(build_parser().parse_args(["conformance"]))

    assert rc == 0
    assert captured["args"] == [str(conformance_dir), "-v"]
    report = json.loads((tmp_path / "conformance-report.json").read_text())
    assert report == {"target": "self", "passed": True}


def test_conformance_reports_failure_when_pytest_main_returns_nonzero(tmp_path, monkeypatch):
    from orlog import cli as cli_module

    conformance_dir = tmp_path / "conformance"
    conformance_dir.mkdir()
    monkeypatch.setattr(cli_module, "_conformance_dir", lambda: conformance_dir)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pytest, "main", lambda pytest_args: 1)

    rc = cli_module.cmd_conformance(build_parser().parse_args(["conformance"]))

    assert rc == 1
    report = json.loads((tmp_path / "conformance-report.json").read_text())
    assert report["passed"] is False


def test_conformance_fails_clearly_when_the_suite_directory_is_missing(tmp_path, monkeypatch, capsys):
    from orlog import cli as cli_module

    missing_dir = tmp_path / "nope"
    monkeypatch.setattr(cli_module, "_conformance_dir", lambda: missing_dir)

    rc = cli_module.cmd_conformance(build_parser().parse_args(["conformance"]))

    assert rc == 2
    assert "conformance suite not found" in capsys.readouterr().err
