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
    """NOTE on the default `embedder` + its `min_confidence`/`ambiguity_margin`:
    spec §B3 mandates fastembed's BAAI/bge-small-en-v1.5 as the default
    embedder (local, no API) -- that's what a fresh `orlog.toml` gets. The
    thresholds below are calibrated for THIS model (ad hoc calibration:
    unrelated queries scored ~0.21-0.29 cosine, real matches ~0.38-0.53; a
    named disambiguating detail still only pulled a runner-up down to
    ~76-89% of the top score, vs ~95%+ for a genuinely ambiguous query with
    no detail mentioned) -- 0.35 and 0.90 sit clearly on the right side of
    both gaps.

    `min_confidence` was re-calibrated from 0.35 to 0.47 after
    retrieval_hybrid.py's `_lexical_score` gained stopword filtering (see
    `_STOPWORDS`): that fix removed a large source of spurious score
    inflation from shared function words (e.g. "is", the split possessive
    "s"), so blended scores across the board -- both real matches and
    unrelated ones -- came down, leaving 0.35 too permissive to filter
    anything (benchmarks/vs_mem0's own "unknown" questions all cleared it).
    0.47 was picked empirically against benchmarks/vs_mem0's dataset: it
    sits strictly below every genuine match's score there (so no known-fact
    accuracy is lost) while sitting above most -- not all -- of the
    should-abstain scores. This is a real, inherent precision/recall
    tradeoff, not a fully-solved separation: a handful of ad hoc
    differently-phrased-but-genuinely-related queries (e.g. "When did X
    start their job?" against a fact phrased as "X joined ... in <date>")
    scored well under this threshold too, in testing outside the benchmark
    dataset. retrieval.py's own docstring already says retrieval quality is
    explicitly outside orlog's trust guarantee (only validity/exclusion
    honesty is normative) -- this threshold is a tunable knob on that
    quality, not a correctness guarantee, and a deployment with different
    query phrasing patterns may need to recalibrate it.

    A cold build of the real model is network-bound (first-run model
    download); `embedder_build_timeout_s` bounds that so recall() abstains
    (EMBEDDER_UNAVAILABLE) rather than hangs -- see runtime.py's module
    docstring point 3.

    `HashingEmbedder` (set `embedder = "hashing"`) remains available as a
    dependency-free, offline stand-in -- what this project's own test suite
    runs against, and a reasonable choice for a workspace that can't afford
    a model download at all. Its cosine channel is crc32 trigram-hash
    noise, not real semantic similarity (see its own docstring in
    retrieval_hybrid.py), so the 0.35/0.90 thresholds below are too strict
    for it -- a workspace that opts into "hashing" should also relax
    min_confidence/ambiguity_margin back down (0.15/0.85 is what earlier
    ad hoc testing found reasonable for it).
    """

    model_config = ConfigDict(extra="forbid")
    k: int = 8
    embedder: str = "BAAI/bge-small-en-v1.5"  # a fastembed model name (spec default) | "hashing" (dependency-free, offline)
    min_confidence: float = 0.47  # below this blended score, a free-text match doesn't count (NO_CANDIDATES) -- calibrated for the default embedder
    ambiguity_margin: float = 0.90  # a runner-up within this fraction of the top score makes the match AMBIGUOUS -- calibrated for the default embedder
    embedder_build_timeout_s: float = 20.0  # bounds a cold/network-bound embedder build; past this, recall() abstains rather than hang


class PytestVerifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    test_command: str = "pytest -q"
    map: str = "tests_map.json"


class VerifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: str = "windows"  # "windows" | "pytest" | "module:path.to.Class"
    pytest: PytestVerifierConfig | None = None
    # A hard wall-clock ceiling around each derive+verify attempt in
    # Pipeline.answer() -- independent of (and a backstop for)
    # DeriverConfig.timeout_s, so a read never blocks past this even if the
    # deriver's own timeout enforcement fails to fire. Past this, recall()
    # abstains (VERIFY_TIMEOUT) rather than hangs.
    answer_timeout_s: float = 45.0


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


class SchemaConfig(BaseModel):
    """spec §B10's [schema] table: schema-on-write, opt-in.

    `known_types` is the ONLY thing declared here -- known attributes per
    type are deliberately not config fields at all. The log itself is the
    attribute registry (any attribute already used by a fact of a known
    type is "known"; Runtime._known_attributes_for_type() derives this by
    scanning), so there is no second, separately-maintained list to drift
    out of sync with what was actually written. An empty list (the default)
    disables schema-on-write entirely -- required for backward compatibility,
    since the existing test suite (and any pre-existing workspace) freely
    mixes "type:label" and bare, untyped entity strings with nothing
    pre-registered.
    """

    model_config = ConfigDict(extra="forbid")
    known_types: list[str] = Field(default_factory=list)


class OrlogConfig(BaseModel):
    """The full orlog.toml schema.

    `schema_` (aliased to the TOML table name "schema") is not called
    `schema` directly: pydantic v2 accepts a field literally named `schema`
    but emits "Field name 'schema' shadows an attribute in parent
    'BaseModel'" on every import -- `populate_by_name=True` lets Python code
    construct via the `schema_=` keyword too (not just the alias), and
    save_config() dumps `by_alias=True` so orlog.toml still reads/writes the
    table as plain `[schema]`.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    workspace: WorkspaceConfig
    deriver: DeriverConfig = Field(default_factory=DeriverConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    verifier: VerifierConfig = Field(default_factory=VerifierConfig)
    projections: ProjectionsConfig = Field(default_factory=ProjectionsConfig)
    adaptation: AdaptationConfig = Field(default_factory=AdaptationConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    schema_: SchemaConfig = Field(default_factory=SchemaConfig, alias="schema")


def load_config(path: Path | str) -> OrlogConfig:
    """Read and validate orlog.toml."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return OrlogConfig.model_validate(data)


def save_config(config: OrlogConfig, path: Path | str) -> None:
    """Write orlog.toml."""
    # by_alias=True: OrlogConfig.schema_ (Python attribute, see its own
    # docstring) must round-trip as the TOML table "schema", not "schema_".
    data = config.model_dump(mode="json", exclude_none=True, by_alias=True)
    with open(path, "wb") as f:
        tomli_w.dump(data, f)
