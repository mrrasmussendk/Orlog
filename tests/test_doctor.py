"""Contract: `orlog doctor` names the specific thing that is wrong, and
exits non-zero only when something is actually broken.

Each check corresponds to a real failure mode that is invisible from inside
an MCP client (see doctor.py's module docstring), so each one is tested for
the state it is supposed to catch AND for not crying wolf on a healthy
install.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import pytest

from orlog.cli import build_parser, cmd_doctor, cmd_init
from orlog.config import OrlogConfig, WorkspaceConfig, save_config
from orlog.doctor import (
    FAIL,
    OK,
    WARN,
    check_deriver,
    check_embedder,
    check_lock,
    check_mcp_clients,
    check_runtime,
    check_vault_key,
    check_workspace,
    format_checks,
    run_checks,
)
from orlog.mcp_clients import CLIENTS, config_path_for
from orlog.vault import VAULT_KEY_ENV, generate_key
from orlog.workspace import Workspace


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    """doctor reads every MCP client's config; keep it inside a tmp HOME so
    it reports on this test's files and not the developer's."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("orlog.mcp_clients._home", lambda: home)
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    return home


@pytest.fixture
def workspace(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.scaffold()
    save_config(OrlogConfig(workspace=WorkspaceConfig(name="ws")), ws.config_path)
    return ws


def _by_name(checks, name):
    return next(c for c in checks if c.name == name)


# --- runtime ---------------------------------------------------------------


def test_the_runtime_check_passes_on_a_supported_python():
    check = check_runtime()

    assert check.status == OK
    assert str(sys.version_info.major) in check.detail


def test_the_runtime_check_fails_below_the_declared_floor(monkeypatch):
    monkeypatch.setattr("orlog.doctor.MIN_PYTHON", (99, 0))

    assert check_runtime().status == FAIL


# --- the vault key ---------------------------------------------------------


def test_an_unset_vault_key_warns_and_explains_why_an_agent_still_works(monkeypatch):
    monkeypatch.delenv(VAULT_KEY_ENV, raising=False)

    check = check_vault_key()

    assert check.status == WARN
    assert "--client" in check.detail


def test_a_malformed_vault_key_fails_rather_than_waiting_for_serve(monkeypatch):
    monkeypatch.setenv(VAULT_KEY_ENV, "not-a-key")

    check = check_vault_key()

    assert check.status == FAIL
    assert VAULT_KEY_ENV in check.detail


def test_a_real_vault_key_passes(monkeypatch):
    monkeypatch.setenv(VAULT_KEY_ENV, generate_key())

    assert check_vault_key().status == OK


# --- the workspace ---------------------------------------------------------


def test_a_directory_with_no_workspace_warns_and_names_the_command(tmp_path):
    check, config = check_workspace(tmp_path / "nothing-here")

    assert check.status == WARN
    assert "orlog init" in check.detail
    assert config is None


def test_a_healthy_workspace_reports_its_name_and_log_size(workspace):
    check, config = check_workspace(workspace.root)

    assert check.status == OK
    assert "name='ws'" in check.detail
    assert config is not None


def test_an_unloadable_config_fails_with_the_parse_error(workspace):
    workspace.config_path.write_text("[workspace]\nname = \n", encoding="utf-8")

    check, config = check_workspace(workspace.root)

    assert check.status == FAIL
    assert str(workspace.config_path) in check.detail
    assert config is None


# --- the writer lock -------------------------------------------------------


def test_a_free_lock_passes(workspace):
    assert check_lock(workspace.root).status == OK


def test_a_lock_held_by_a_live_process_warns_and_names_the_http_escape_hatch(workspace):
    workspace.lock_path.write_text(str(os.getpid()), encoding="utf-8")

    check = check_lock(workspace.root)

    assert check.status == WARN
    assert "E_LOCKED" in check.detail
    assert "--http" in check.detail


def test_a_stale_lock_is_reported_as_reclaimable(workspace, monkeypatch):
    workspace.lock_path.write_text("999999", encoding="utf-8")
    monkeypatch.setattr("orlog.workspace._pid_alive", lambda pid: False)

    check = check_lock(workspace.root)

    assert check.status == OK
    assert "reclaim" in check.detail


# --- the embedder ----------------------------------------------------------


def test_the_hashing_embedder_needs_nothing_and_says_so():
    config = OrlogConfig(workspace=WorkspaceConfig(name="w"))
    config.retrieval.embedder = "hashing"

    check = check_embedder(config)

    assert check.status == OK
    assert "offline" in check.detail


def test_a_missing_fastembed_fails_and_names_both_ways_out(monkeypatch):
    monkeypatch.setattr("orlog.doctor._installed", lambda module: False)
    config = OrlogConfig(workspace=WorkspaceConfig(name="w"))

    check = check_embedder(config)

    assert check.status == FAIL
    assert "pip install fastembed" in check.detail
    assert "hashing" in check.detail
    assert "EMBEDDER_UNAVAILABLE" in check.detail


def test_an_uncached_model_warns_that_the_first_recall_downloads_it(monkeypatch, tmp_path):
    monkeypatch.setattr("orlog.doctor._installed", lambda module: True)
    monkeypatch.setattr("orlog.retrieval_hybrid._default_fastembed_cache_dir", lambda: tmp_path / "cold")
    config = OrlogConfig(workspace=WorkspaceConfig(name="w"))

    check = check_embedder(config)

    assert check.status == WARN
    assert "downloads it" in check.detail


def test_a_cached_model_passes(monkeypatch, tmp_path):
    cache = tmp_path / "warm"
    cache.mkdir()
    (cache / "model.onnx").write_text("x", encoding="utf-8")
    monkeypatch.setattr("orlog.doctor._installed", lambda module: True)
    monkeypatch.setattr("orlog.retrieval_hybrid._default_fastembed_cache_dir", lambda: cache)

    assert check_embedder(OrlogConfig(workspace=WorkspaceConfig(name="w"))).status == OK


# --- the deriver -----------------------------------------------------------


def test_the_scripted_deriver_needs_no_key():
    check = check_deriver(OrlogConfig(workspace=WorkspaceConfig(name="w")))

    assert check.status == OK
    assert "no API key" in check.detail


def test_an_llm_deriver_without_its_api_key_fails(monkeypatch):
    monkeypatch.setattr("orlog.doctor._installed", lambda module: True)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = OrlogConfig(workspace=WorkspaceConfig(name="w"))
    config.deriver.backend = "anthropic"

    check = check_deriver(config)

    assert check.status == FAIL
    assert "ANTHROPIC_API_KEY is not set" in check.detail


def test_an_llm_deriver_without_its_sdk_fails_and_names_the_extra(monkeypatch):
    monkeypatch.setattr("orlog.doctor._installed", lambda module: False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    config = OrlogConfig(workspace=WorkspaceConfig(name="w"))
    config.deriver.backend = "openai"
    config.deriver.api_key_env = "OPENAI_API_KEY"

    check = check_deriver(config)

    assert check.status == FAIL
    assert "orlog[openai]" in check.detail


def test_a_fully_configured_llm_deriver_passes(monkeypatch):
    monkeypatch.setattr("orlog.doctor._installed", lambda module: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    config = OrlogConfig(workspace=WorkspaceConfig(name="w"))
    config.deriver.backend = "anthropic"

    assert check_deriver(config).status == OK


# --- the MCP clients -------------------------------------------------------


def test_no_client_config_at_all_warns_and_names_the_init_flag():
    checks = check_mcp_clients()

    assert [c.status for c in checks] == [WARN]
    assert "--client=claude-code" in checks[0].detail


def test_a_configured_client_with_no_orlog_server_warns(fake_home):
    path = config_path_for(CLIENTS["cursor"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "/bin/other"}}}), encoding="utf-8")

    check = _by_name(check_mcp_clients(), "mcp: cursor")

    assert check.status == WARN
    assert "--client=cursor" in check.detail


def test_a_registered_server_passes_and_reports_its_workspace(tmp_path, monkeypatch, fake_home):
    root = tmp_path / "myproject"
    monkeypatch.setattr("orlog.cli._default_claude_code_config_path", lambda: fake_home / ".claude.json")
    cmd_init(build_parser().parse_args(["init", str(root), "--client=cursor"]))
    # cmd_init embeds the real `orlog` path; this test only cares that the
    # check follows it, so point it at something that exists.
    path = config_path_for(CLIENTS["cursor"])
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mcpServers"]["orlog"]["command"] = sys.executable
    path.write_text(json.dumps(data), encoding="utf-8")

    check = _by_name(check_mcp_clients(), "mcp: cursor")

    assert check.status == OK
    assert str(root.resolve()) in check.detail


def test_a_server_pointing_at_a_vanished_executable_fails(fake_home, tmp_path):
    path = config_path_for(CLIENTS["cursor"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "orlog": {
                        "command": str(tmp_path / "gone" / "orlog"),
                        "args": ["serve", str(tmp_path / "ws")],
                        "env": {VAULT_KEY_ENV: "k"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    check = _by_name(check_mcp_clients(), "mcp: cursor")

    assert check.status == FAIL
    assert "no longer exists" in check.detail
    assert "orlog init" in check.detail


def test_a_server_registered_without_a_vault_key_fails(fake_home):
    path = config_path_for(CLIENTS["cursor"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"mcpServers": {"orlog": {"command": sys.executable, "args": ["serve", "/ws"], "env": {}}}}
        ),
        encoding="utf-8",
    )

    check = _by_name(check_mcp_clients(), "mcp: cursor")

    assert check.status == FAIL
    assert "cannot open the vault" in check.detail


def test_an_unreadable_client_config_is_reported_not_swallowed(fake_home):
    path = config_path_for(CLIENTS["cursor"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    check = _by_name(check_mcp_clients(), "mcp: cursor")

    assert check.status == FAIL
    assert "not valid JSON" in check.detail


# --- the command itself ----------------------------------------------------


def test_doctor_exits_non_zero_when_something_is_broken(workspace, monkeypatch, capsys):
    monkeypatch.setenv(VAULT_KEY_ENV, "not-a-key")

    rc = cmd_doctor(argparse.Namespace(path=str(workspace.root)))

    assert rc == 1
    assert "problem(s) to fix" in capsys.readouterr().out


def test_doctor_exits_zero_when_only_warnings_remain(workspace, monkeypatch, capsys):
    monkeypatch.setenv(VAULT_KEY_ENV, generate_key())
    monkeypatch.setattr("orlog.doctor._installed", lambda module: True)
    monkeypatch.setattr("orlog.doctor.check_mcp_clients", lambda: [])

    rc = cmd_doctor(argparse.Namespace(path=str(workspace.root)))

    assert rc == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out


def test_doctor_runs_against_a_directory_that_is_not_a_workspace(tmp_path):
    checks = run_checks(tmp_path / "empty")

    assert _by_name(checks, "workspace").status == WARN
    # No workspace means no config, so the lock check has nothing to say.
    assert not any(c.name == "writer lock" for c in checks)


def test_the_report_marks_every_status_and_ends_with_a_verdict(workspace, monkeypatch):
    monkeypatch.setenv(VAULT_KEY_ENV, generate_key())

    text = format_checks(run_checks(workspace.root))

    assert text.splitlines()[0].startswith("[ok")
    assert text.strip().splitlines()[-1]
