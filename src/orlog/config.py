"""config: orlog.toml -- workspace configuration (ORLOG-SPEC.md §B10).

Design decisions:

1. One pydantic model per TOML table, nested to match the file's own
   structure ([deriver], [projections.supersession_chains], ...) -- so a
   validation error's field path (e.g. "deriver.max_tokens") reads exactly
   like the TOML key path a user would fix.

2. tomllib is stdlib from Python 3.11, but this project currently runs on
   3.10 (see pyproject.toml's own note on that), so reading falls back to
   the `tomli` backport, which has an identical API. Writing uses `tomli_w`
   (there is no stdlib TOML writer, on any version).

3. save_config() never writes secrets: DeriverConfig.api_key_env is only
   the NAME of an environment variable, never a key value, so orlog.toml
   stays safe to commit (spec §B7: "orlog.toml MUST be committable").
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover -- this project's actual interpreter is 3.10
    import tomli as tomllib

import tomli_w

DEFAULT_CONFIG_FILENAME = "orlog.toml"


class WorkspaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    mode: str = "conformant"  # "conformant" | "degraded"


class DeriverConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: str = "scripted"  # "anthropic" | "openai" | "scripted"
    model: str = "claude-haiku-4-5"
    api_key_env: str = "ANTHROPIC_API_KEY"
    max_tokens: int = 400
    timeout_s: int = 30


class RetrievalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    k: int = 8
    embedder: str = "BAAI/bge-small-en-v1.5"


class PytestVerifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    test_command: str = "pytest -q"
    map: str = "tests_map.json"


class VerifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: str = "windows"  # "windows" | "pytest" | "module:path.to.Class"
    pytest: PytestVerifierConfig | None = None


class SupersessionChainsProjectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grouping: str = "entity+type"  # or "llm"
    keep_versions: int = 2


class ProjectionsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supersession_chains: SupersessionChainsProjectionConfig = Field(default_factory=SupersessionChainsProjectionConfig)


class AdaptationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pass_bonus: float = 0.5
    correction_bonus: float = 1.0
    daily_decay: float = 0.99


class CacheConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_entries: int = 10_000


class PrivacyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detectors: list[str] = Field(default_factory=lambda: ["regex"])
    resolve_entities: bool = False


class OrlogConfig(BaseModel):
    """The full orlog.toml schema."""

    model_config = ConfigDict(extra="forbid")

    workspace: WorkspaceConfig
    deriver: DeriverConfig = Field(default_factory=DeriverConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    verifier: VerifierConfig = Field(default_factory=VerifierConfig)
    projections: ProjectionsConfig = Field(default_factory=ProjectionsConfig)
    adaptation: AdaptationConfig = Field(default_factory=AdaptationConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)


def load_config(path: Path | str) -> OrlogConfig:
    """Read and validate orlog.toml."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return OrlogConfig.model_validate(data)


def save_config(config: OrlogConfig, path: Path | str) -> None:
    """Write orlog.toml."""
    data = config.model_dump(mode="json", exclude_none=True)
    with open(path, "wb") as f:
        tomli_w.dump(data, f)
