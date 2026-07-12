"""Contract: each CLI subcommand (except serve/conformance, which spawn a
process or a subprocess) works end to end against a real scratch
workspace -- ORLOG-SPEC.md §B1.
"""

from datetime import datetime, timezone

import pytest

from orlog.cli import build_parser, cmd_forget, cmd_init, cmd_inspect, cmd_replay
from orlog.config import load_config
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
