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
import tempfile
import tomllib
from pathlib import Path

import pytest

from orlog.cli import build_parser, cmd_init
from orlog.mcp_clients import (
    CLIENTS,
    ClientConfigError,
    config_path_for,
    ephemeral_command_reason,
    orlog_command,
    register,
    registered_servers,
)
from orlog.vault import VAULT_KEY_ENV
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


def test_init_on_an_existing_workspace_reuses_the_key_already_on_record(tmp_path, monkeypatch, capsys):
    # A second client, registered later. The workspace's vault is encrypted
    # under the key the FIRST registration recorded, so a fresh one could
    # not open it -- the repair has to recover the real one.
    monkeypatch.delenv(VAULT_KEY_ENV, raising=False)
    root = tmp_path / "myproject"
    cmd_init(build_parser().parse_args(["init", str(root), "--client=cursor"]))
    original = registered_servers("cursor")["orlog"]["env"][VAULT_KEY_ENV]

    rc = cmd_init(build_parser().parse_args(["init", str(root), "--client=vscode"]))

    assert rc == 0
    assert registered_servers("vscode")["orlog"]["env"][VAULT_KEY_ENV] == original
    assert "already on record" in capsys.readouterr().out


def test_init_repairs_a_client_entry_whose_orlog_has_moved(tmp_path, monkeypatch, capsys):
    # This is the command `orlog doctor` prints when it finds a stale entry.
    # It used to do nothing at all.
    monkeypatch.delenv(VAULT_KEY_ENV, raising=False)
    root = tmp_path / "myproject"
    cmd_init(build_parser().parse_args(["init", str(root), "--client=cursor"]))
    path = config_path_for(CLIENTS["cursor"])
    data = json.loads(path.read_text(encoding="utf-8"))
    key = data["mcpServers"]["orlog"]["env"][VAULT_KEY_ENV]
    data["mcpServers"]["orlog"]["command"] = str(tmp_path / "gone" / "orlog")
    path.write_text(json.dumps(data), encoding="utf-8")

    rc = cmd_init(build_parser().parse_args(["init", str(root), "--client=cursor"]))

    assert rc == 0
    repaired = registered_servers("cursor")["orlog"]
    assert repaired["command"] == orlog_command(), "the stale command must be re-pointed"
    assert repaired["env"][VAULT_KEY_ENV] == key, "and the original key must survive the repair"


def test_init_on_an_existing_workspace_with_no_recoverable_key_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(VAULT_KEY_ENV, raising=False)
    root = tmp_path / "myproject"
    cmd_init(build_parser().parse_args(["init", str(root)]))  # no client: key only ever printed

    rc = cmd_init(build_parser().parse_args(["init", str(root), "--client=cursor"]))

    assert rc == 0
    assert registered_servers("cursor") == {}, "better to write nothing than a key that cannot open the vault"
    assert "nothing to re-register with" in capsys.readouterr().out


def test_init_on_an_existing_workspace_falls_back_to_the_exported_key(tmp_path, monkeypatch, capsys):
    root = tmp_path / "myproject"
    cmd_init(build_parser().parse_args(["init", str(root)]))
    monkeypatch.setenv(VAULT_KEY_ENV, "the-exported-key")

    rc = cmd_init(build_parser().parse_args(["init", str(root), "--client=cursor"]))

    assert rc == 0
    assert registered_servers("cursor")["orlog"]["env"][VAULT_KEY_ENV] == "the-exported-key"
    assert f"${VAULT_KEY_ENV}" in capsys.readouterr().out, "say where the key came from"


def test_registering_another_workspace_under_the_same_name_is_refused(ws, tmp_path):
    # That env block is the only copy of the first workspace's vault key,
    # so replacing it would make its encrypted registry unrecoverable.
    register("cursor", ws, "the-first-key")
    other = Workspace(tmp_path / "second")
    other.scaffold()

    with pytest.raises(ClientConfigError, match="different workspace"):
        register("cursor", other, "a-second-key")

    assert registered_servers("cursor")["orlog"]["env"][VAULT_KEY_ENV] == "the-first-key"


def test_re_registering_the_same_workspace_is_allowed(ws):
    register("cursor", ws, "k")

    register("cursor", ws, "k", command="/new/path/orlog")

    assert registered_servers("cursor")["orlog"]["command"] == "/new/path/orlog"


def test_codex_distinguishes_a_repair_from_someone_elses_workspace(ws, tmp_path):
    register("codex", ws, "k")
    other = Workspace(tmp_path / "second")
    other.scaffold()

    with pytest.raises(ClientConfigError, match="for this workspace"):
        register("codex", ws, "k")
    with pytest.raises(ClientConfigError, match="different workspace"):
        register("codex", other, "k2")


def test_the_registered_command_keeps_the_symlink_homebrew_upgrades(tmp_path, monkeypatch):
    # /opt/homebrew/bin/orlog -> ../Cellar/orlog/<version>/bin/orlog.
    # Resolving that records the version, and `brew upgrade` deletes it.
    cellar = tmp_path / "Cellar" / "orlog" / "0.0.4" / "bin"
    cellar.mkdir(parents=True)
    (cellar / "orlog").write_text("#!/bin/sh\n", encoding="utf-8")
    launcher = tmp_path / "bin"
    launcher.mkdir()
    (launcher / "orlog").symlink_to(cellar / "orlog")
    monkeypatch.setattr("orlog.mcp_clients.sys.argv", [str(launcher / "orlog"), "init"])

    assert orlog_command() == str(launcher / "orlog")
    assert "Cellar" not in orlog_command()


def test_a_uv_tool_cache_command_is_reported_as_not_durable(tmp_path):
    reason = ephemeral_command_reason(str(tmp_path / ".cache" / "uv" / "archive-v0" / "abc" / "bin" / "orlog"))

    assert reason and "uv cache clean" in reason


def test_a_command_under_the_temp_directory_is_reported_as_not_durable():
    reason = ephemeral_command_reason(str(Path(tempfile.gettempdir()) / "build" / "orlog"))

    assert reason and "temporary directory" in reason


def test_an_installed_command_is_not_reported_as_ephemeral():
    assert ephemeral_command_reason("/usr/local/bin/orlog") is None
    assert ephemeral_command_reason("") is None


def test_init_warns_when_it_registers_a_command_that_will_not_last(tmp_path, monkeypatch, capsys):
    cache = tmp_path / ".cache" / "uv" / "archive-v0" / "deadbeef" / "bin"
    cache.mkdir(parents=True)
    (cache / "orlog").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("orlog.mcp_clients.sys.argv", [str(cache / "orlog"), "init"])

    cmd_init(build_parser().parse_args(["init", str(tmp_path / "proj"), "--client=cursor"]))

    assert "uv cache clean" in capsys.readouterr().err


def test_init_reports_an_unwritable_config_instead_of_crashing(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr("orlog.cli.register", boom)

    rc = cmd_init(build_parser().parse_args(["init", str(tmp_path / "proj"), "--client=cursor"]))

    assert rc == 2
    err = capsys.readouterr().err
    assert "Permission denied" in err
    # The workspace exists now, so losing the key would strand it.
    assert f"{VAULT_KEY_ENV}=" in err
