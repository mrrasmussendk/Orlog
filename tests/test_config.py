"""Contract: orlog.toml round-trips through load/save, and every section
has spec-matching defaults so a minimal [workspace] table is enough to get
a fully valid config.
"""

from orlog.config import OrlogConfig, WorkspaceConfig, load_config, save_config


def test_minimal_config_gets_full_spec_defaults():
    config = OrlogConfig(workspace=WorkspaceConfig(name="my-project"))

    assert config.workspace.mode == "conformant"
    assert config.deriver.backend == "scripted"
    assert config.deriver.max_tokens == 400
    assert config.retrieval.k == 8
    assert config.retrieval.embedder == "BAAI/bge-small-en-v1.5"
    assert config.retrieval.min_confidence == 0.35
    assert config.retrieval.ambiguity_margin == 0.90
    assert config.verifier.backend == "windows"
    assert config.projections.supersession_chains.grouping == "entity+type"
    assert config.adaptation.pass_bonus == 0.5
    assert config.cache.max_entries == 10_000
    assert config.privacy.detectors == ["regex"]
    assert config.privacy.resolve_entities is False


def test_save_then_load_round_trips(tmp_path):
    config = OrlogConfig(workspace=WorkspaceConfig(name="my-project", mode="degraded"))
    config.adaptation.pass_bonus = 0.75

    path = tmp_path / "orlog.toml"
    save_config(config, path)
    loaded = load_config(path)

    assert loaded == config


def test_saved_config_never_contains_a_literal_api_key(tmp_path):
    # api_key_env only ever names an environment variable, never a value --
    # this test is really just confirming that field is what gets written,
    # not some other "api_key" field a future edit might accidentally add.
    config = OrlogConfig(workspace=WorkspaceConfig(name="my-project"))
    path = tmp_path / "orlog.toml"
    save_config(config, path)

    text = path.read_text(encoding="utf-8")
    assert 'api_key_env = "ANTHROPIC_API_KEY"' in text
    assert "sk-" not in text  # no accidental literal secret-shaped value
