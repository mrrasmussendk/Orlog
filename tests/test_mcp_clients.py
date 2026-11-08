"""Contract: `orlog init --client=<name>` registers the workspace in the
config file of each supported MCP client, preserving everything else in
that file -- the last step of the install, and the one that writes the
vault key.

Every test here redirects HOME (orlog.mcp_clients._home) at a tmp_path:
these functions edit real editor config files, and a test suite that can
reach the developer's own ~/.claude.json is a bug waiting to happen.
"""

from __future__ import annotations

import argparse
import json
import tomllib

import pytest

from orlog.cli import build_parser, cmd_init
from orlog.mcp_clients import (
    CLIENTS,
    ClientConfigError,
    config_path_for,
    orlog_command,
    register,
    registered_servers,
)
from orlog.vault import VAULT_KEY_ENV, generate_key
from orlog.workspace import Workspace


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("orlog.mcp_clients._home", lambda: home)
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    return home


@pytest.fixture
def ws(tmp_path):
    workspace = Workspace(tmp_path / "myproject")
    workspace.scaffold()
    return workspace


@pytest.mark.parametrize("client_key", sorted(CLIENTS))
@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_every_client_resolves_a_config_path_on_every_platform(client_key, platform, monkeypatch, fake_home):
    monkeypatch.setattr("orlog.mcp_clients.sys.platform", platform)
    path = config_path_for(CLIENTS[client_key])
    # Under HOME (or %APPDATA%, which this suite also points under HOME), so
    # a client whose per-platform entry was left out of the registry fails
    # here rather than silently writing somewhere unexpected.
    assert str(path).startswith(str(fake_home))
    assert path.name


@pytest.mark.parametrize("client_key", sorted(set(CLIENTS) - {"codex"}))
def test_registering_writes_a_spawnable_stdio_server_with_the_vault_key(client_key, ws):
    path = register(client_key, ws, "the-vault-key")

    assert path.exists()
    servers = registered_servers(client_key)
    assert list(servers) == ["orlog"]
    entry = servers["orlog"]
    assert entry["command"] == orlog_command()
    assert entry["args"] == ["serve", str(ws.root.resolve())]
    assert entry["env"] == {VAULT_KEY_ENV: "the-vault-key"}


def test_the_registered_command_is_the_orlog_that_is_running(tmp_path, monkeypatch):
    # Two installs on one machine: the one being invoked, and another that
    # happens to come first on PATH. Registering the PATH one points the
    # agent at different code, and silently -- that path exists, so doctor
    # calls it fine.
    invoked = tmp_path / "venv" / "bin" / "orlog"
    invoked.parent.mkdir(parents=True)
    invoked.write_text("#!/bin/sh\n", encoding="utf-8")
    other = tmp_path / "usr" / "local" / "bin" / "orlog"
    other.parent.mkdir(parents=True)
    other.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("orlog.mcp_clients.sys.argv", [str(invoked), "init"])
    monkeypatch.setattr("orlog.mcp_clients.shutil.which", lambda name: str(other))

    assert orlog_command() == str(invoked)


def test_the_registered_command_falls_back_to_this_interpreters_console_script(tmp_path, monkeypatch):
    # `python -m orlog.cli`: argv[0] is cli.py, so the console script
    # installed beside this interpreter is the right answer -- still not
    # whatever PATH happens to resolve first.
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "orlog").write_text("#!/bin/sh\n", encoding="utf-8")
    other = tmp_path / "elsewhere" / "orlog"
    other.parent.mkdir(parents=True)
    other.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("orlog.mcp_clients.sys.argv", [str(tmp_path / "src" / "orlog" / "cli.py")])
    monkeypatch.setattr("orlog.mcp_clients.sys.executable", str(bin_dir / "python3"))
    monkeypatch.setattr("orlog.mcp_clients.sys.platform", "linux")
    monkeypatch.setattr("orlog.mcp_clients.shutil.which", lambda name: str(other))

    assert orlog_command() == str(bin_dir / "orlog")


def test_the_registered_command_uses_path_only_as_a_last_resort(tmp_path, monkeypatch):
    on_path = tmp_path / "somewhere" / "orlog"
    on_path.parent.mkdir(parents=True)
    on_path.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("orlog.mcp_clients.sys.argv", [str(tmp_path / "nope" / "cli.py")])
    monkeypatch.setattr("orlog.mcp_clients.sys.executable", str(tmp_path / "nope" / "python3"))
    monkeypatch.setattr("orlog.mcp_clients.shutil.which", lambda name: str(on_path))

    assert orlog_command() == str(on_path)


def test_vscode_uses_its_own_servers_key_and_type_discriminator(ws):
    path = register("vscode", ws, "k")

    data = json.loads(path.read_text(encoding="utf-8"))
    assert "mcpServers" not in data
    assert data["servers"]["orlog"]["type"] == "stdio"


def test_claude_desktop_omits_the_type_discriminator(ws):
    path = register("claude-desktop", ws, "k")

    entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["orlog"]
    assert "type" not in entry


def test_registering_preserves_unrelated_settings_and_other_servers(ws):
    path = config_path_for(CLIENTS["cursor"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"someOtherSetting": True, "mcpServers": {"other": {"command": "x"}}}),
        encoding="utf-8",
    )

    register("cursor", ws, "k")

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["someOtherSetting"] is True
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert "orlog" in data["mcpServers"]
    assert path.with_suffix(".json.bak").exists()


def test_registering_refuses_a_config_that_is_not_valid_json(ws):
    path = config_path_for(CLIENTS["cursor"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ClientConfigError, match="not valid JSON"):
        register("cursor", ws, "k")


def test_registering_refuses_a_servers_key_that_is_not_an_object(ws):
    path = config_path_for(CLIENTS["cursor"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": ["not", "an", "object"]}), encoding="utf-8")

    with pytest.raises(ClientConfigError, match="not an object"):
        register("cursor", ws, "k")


def test_an_unknown_client_names_the_ones_that_exist(ws):
    with pytest.raises(ClientConfigError, match="claude-code"):
        register("emacs", ws, "k")


def test_a_second_workspace_can_be_registered_under_another_server_name(ws, tmp_path):
    other = Workspace(tmp_path / "second")
    other.scaffold()

    register("cursor", ws, "k1")
    register("cursor", other, "k2", server_name="orlog-second")

    servers = registered_servers("cursor")
    assert sorted(servers) == ["orlog", "orlog-second"]
    assert servers["orlog-second"]["args"][1] == str(other.root.resolve())


# --- Codex: TOML, appended rather than rewritten ---------------------------


def test_codex_registration_appends_a_table_and_keeps_comments_byte_for_byte(ws):
    path = config_path_for(CLIENTS["codex"])
    path.parent.mkdir(parents=True, exist_ok=True)
    original = '# my hand-written config, with comments\nmodel = "gpt-5"\n'
    path.write_text(original, encoding="utf-8")

    register("codex", ws, "the-vault-key")

    text = path.read_text(encoding="utf-8")
    assert text.startswith(original)
    data = tomllib.loads(text)
    assert data["model"] == "gpt-5"
    entry = data["mcp_servers"]["orlog"]
    assert entry["command"] == orlog_command()
    assert entry["args"] == ["serve", str(ws.root.resolve())]
    assert entry["env"] == {VAULT_KEY_ENV: "the-vault-key"}
    # Codex infers stdio from `command`; the JSON clients' discriminator
    # would be an unknown key here.
    assert "type" not in entry


def test_codex_registration_works_from_no_config_at_all(ws):
    register("codex", ws, "k")

    assert registered_servers("codex")["orlog"]["env"] == {VAULT_KEY_ENV: "k"}


def test_codex_registration_refuses_to_replace_an_existing_orlog_table(ws):
    path = config_path_for(CLIENTS["codex"])
    path.parent.mkdir(parents=True, exist_ok=True)
    original = '[mcp_servers.orlog]\ncommand = "/somewhere/else/orlog"\n'
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ClientConfigError, match="already has"):
        register("codex", ws, "k")
    assert path.read_text(encoding="utf-8") == original


def test_codex_registration_rolls_back_when_the_append_would_break_the_file(ws):
    # An inline root-level table: it parses, has no `orlog` inside it, and
    # yet appending [mcp_servers.orlog] redefines a key TOML has already
    # closed. The file must come back exactly as it was.
    path = config_path_for(CLIENTS["codex"])
    path.parent.mkdir(parents=True, exist_ok=True)
    original = 'mcp_servers = { other = { command = "x" } }\n'
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ClientConfigError, match="invalid TOML"):
        register("codex", ws, "k")
    assert path.read_text(encoding="utf-8") == original


def test_codex_registration_refuses_a_config_that_is_not_valid_toml(ws):
    path = config_path_for(CLIENTS["codex"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is = = not toml\n", encoding="utf-8")

    with pytest.raises(ClientConfigError, match="not valid TOML"):
        register("codex", ws, "k")


# --- the CLI surface -------------------------------------------------------


@pytest.fixture(autouse=True)
def init_writes_under_fake_home(monkeypatch, fake_home):
    """cmd_init resolves claude-code's path through this seam; keep it
    pointed inside the fake HOME like every other client."""
    monkeypatch.setattr(
        "orlog.cli._default_claude_code_config_path",
        lambda: fake_home / ".claude.json",
    )


def test_init_can_register_several_clients_in_one_run(tmp_path, capsys):
    root = tmp_path / "myproject"

    rc = cmd_init(build_parser().parse_args(["init", str(root), "--client=cursor", "--client=codex"]))

    assert rc == 0
    assert "orlog" in registered_servers("cursor")
    assert "orlog" in registered_servers("codex")
    out = capsys.readouterr().out
    assert "Cursor: reload Cursor" in out
    assert "Codex CLI: start a new Codex session" in out


def test_init_registers_the_same_vault_key_everywhere(tmp_path):
    root = tmp_path / "myproject"

    cmd_init(build_parser().parse_args(["init", str(root), "--client=cursor", "--client=vscode", "--client=codex"]))

    keys = {
        registered_servers("cursor")["orlog"]["env"][VAULT_KEY_ENV],
        registered_servers("vscode")["orlog"]["env"][VAULT_KEY_ENV],
        registered_servers("codex")["orlog"]["env"][VAULT_KEY_ENV],
    }
    assert len(keys) == 1, "one workspace has one vault key, however many clients were told about it"


def test_claude_code_flag_is_shorthand_for_the_client_option(tmp_path, fake_home):
    root = tmp_path / "myproject"

    rc = cmd_init(build_parser().parse_args(["init", str(root), "--claude-code"]))

    assert rc == 0
    assert "orlog" in registered_servers("claude-code", config_path=fake_home / ".claude.json")


def test_init_honours_server_name_for_every_client(tmp_path):
    root = tmp_path / "myproject"

    cmd_init(build_parser().parse_args(["init", str(root), "--client=cursor", "--server-name", "memory"]))

    assert list(registered_servers("cursor")) == ["memory"]


def test_the_parser_rejects_an_unknown_client_outright(tmp_path):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["init", str(tmp_path), "--client=emacs"])


def test_init_rejects_an_unknown_client_before_scaffolding_anything(tmp_path, capsys):
    # argparse's `choices` catches this on the command line; cmd_init is
    # also called directly (cli.py design decision 1), so it checks too.
    root = tmp_path / "myproject"

    rc = cmd_init(
        argparse.Namespace(
            path=str(root), name=None, client=["emacs"], claude_code=False, server_name=None, claude_code_name=None
        )
    )

    assert rc == 2
    assert "unknown MCP client" in capsys.readouterr().err
    assert not root.exists(), "a typo'd --client must not leave a half-built workspace behind"


def test_init_prints_the_vault_key_when_a_client_registration_fails(tmp_path, capsys, monkeypatch):
    root = tmp_path / "myproject"
    path = config_path_for(CLIENTS["cursor"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    rc = cmd_init(build_parser().parse_args(["init", str(root), "--client=cursor"]))

    assert rc == 2
    captured = capsys.readouterr()
    assert "registering with Cursor failed" in captured.err
    # Without the key printed, the workspace that WAS created is unopenable.
    assert f"{VAULT_KEY_ENV}=" in captured.err


def test_init_on_an_existing_workspace_refuses_to_write_a_fresh_key(tmp_path, capsys):
    root = tmp_path / "myproject"
    cmd_init(build_parser().parse_args(["init", str(root)]))
    before = generate_key()  # just to prove the next call invents nothing

    rc = cmd_init(build_parser().parse_args(["init", str(root), "--client=cursor"]))

    assert rc == 0
    assert registered_servers("cursor") == {}
    out = capsys.readouterr().out
    assert "no new vault key was generated" in out
    assert before not in out
