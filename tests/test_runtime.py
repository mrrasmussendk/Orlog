"""Contract: Runtime wires a Workspace + Config into a working remember()
-> build_pipeline() -> answer() round trip.
"""

import time
from datetime import datetime, timezone

import pytest

from orlog.config import OrlogConfig, RetrievalConfig, SchemaConfig, WorkspaceConfig
from orlog.errors import RetrieverUnavailableError, SchemaError
from orlog.retrieval_hybrid import HashingEmbedder
from orlog.runtime import Runtime
from orlog.vault import generate_key
from orlog.workspace import Workspace

T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    monkeypatch.setenv("ORLOG_VAULT_KEY", generate_key())


@pytest.fixture
def runtime(tmp_path):
    ws = Workspace(tmp_path / "myproject")
    # Pinned to "hashing" rather than relying on OrlogConfig's default: the
    # real default (spec §B3's fastembed/bge-small) is network-bound on a
    # cold build, which this Runtime-level test suite avoids (see README's
    # "Known deviations" -- test_retrieval_hybrid.py does exercise the real
    # FastEmbedEmbedder directly, just not through a full Runtime).
    config = OrlogConfig(workspace=WorkspaceConfig(name="myproject"), retrieval=RetrievalConfig(embedder="hashing"))
    rt = Runtime(ws, config)
    yield rt
    rt.close()


def test_remember_appends_a_scrubbed_fact_event(runtime):
    event = runtime.remember(
        "user email is alice@example.com", occurred_at=T1,
        entity="user:1", attribute="email", value="alice@example.com",
    )
    assert "alice@example.com" not in event.payload["text"]
    assert event.payload["entity"] == "user:1"
    # `value` is caller-supplied fact content, not a structured key -- it can
    # carry the same PII shapes `text` can, and must be scrubbed identically
    # (otherwise it lands in the immutable log un-tokenized, unreachable by
    # vault.forget()'s crypto-shredding).
    assert "alice@example.com" not in event.payload["value"]


def test_remembered_value_is_tokenized_and_recoverable_through_the_vault(runtime):
    event = runtime.remember(
        "user email is alice@example.com", occurred_at=T1,
        entity="user:1", attribute="email", value="alice@example.com",
    )
    token = event.payload["value"]
    assert token.startswith("EMAIL_")
    assert runtime.vault.resolve(token) == "alice@example.com"


def test_build_pipeline_returns_none_with_no_fact_events(runtime):
    # A bare-text remember() is rejected outright (see the tests below), so
    # the only way to reach "zero fact events" now is an untouched log.
    assert runtime.build_pipeline(now=NOW) is None


def test_remember_rejects_text_with_no_entity_attribute_or_value(runtime):
    with pytest.raises(SchemaError) as exc_info:
        runtime.remember("just a note", occurred_at=T1)
    assert "entity" in str(exc_info.value)
    assert "attribute" in str(exc_info.value)
    assert "value" in str(exc_info.value)


def test_remember_rejects_a_partial_entity_attribute_value(runtime):
    # entity given without attribute/value used to be silently discarded as
    # if entity had never been passed at all -- now an explicit error.
    with pytest.raises(SchemaError):
        runtime.remember("just a note", occurred_at=T1, entity="user:1")
    with pytest.raises(SchemaError):
        runtime.remember("just a note", occurred_at=T1, entity="user:1", attribute="plan")


def test_remember_then_recall_round_trips(runtime):
    runtime.remember("plan is pro", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    pipeline = runtime.build_pipeline(now=NOW)
    result = pipeline.answer("q1", "user:1", "plan", NOW, now=NOW)

    assert result.verified is True
    assert result.claim == "user:1.plan = pro"


def test_stats_are_shared_across_pipeline_calls(runtime):
    runtime.remember("plan is pro", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    pipeline = runtime.build_pipeline(now=NOW)
    pipeline.answer("q1", "user:1", "plan", NOW, now=NOW)

    assert runtime.stats.snapshot()["appends"] == 1
    assert runtime.stats.snapshot()["recalls"] == 1


def test_index_is_populated_on_remember(runtime):
    runtime.remember("plan is pro", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    rows = runtime.index._conn.execute("SELECT COUNT(*) FROM events_idx").fetchone()
    assert rows[0] == 1


def test_embedder_is_built_once_and_memoized(runtime):
    embedder = runtime.embedder

    assert isinstance(embedder, HashingEmbedder)  # fixture pins config.retrieval.embedder to "hashing"
    assert runtime.embedder is embedder  # lazily built once, not reconstructed per access


def test_embedder_config_defaults_to_the_spec_fastembed_model():
    # OrlogConfig's own default (not the "hashing" pin the `runtime` fixture
    # above uses) must match spec §B3 -- checked here as a plain config
    # value, never by actually building a FastEmbedEmbedder (network-bound).
    config = OrlogConfig(workspace=WorkspaceConfig(name="myproject"))

    assert config.retrieval.embedder == "BAAI/bge-small-en-v1.5"


def test_remember_flags_a_fact_whose_text_contradicts_its_own_value(runtime):
    event = runtime.remember(
        "person trap moved to Paris", occurred_at=T1,
        entity="person:trap", attribute="city", value="Tokyo",
    )

    assert event.payload["self_supported"] is False


def test_remember_flags_a_consistent_fact_as_self_supported(runtime):
    event = runtime.remember("plan is pro", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    assert event.payload["self_supported"] is True


def test_remember_accepts_a_paraphrased_value_grounded_by_a_real_evidence_span(runtime):
    # value doesn't appear verbatim in text at all -- only evidence_span does.
    # Because the span doesn't contain the value either, this is a paraphrase
    # no deterministic check can confirm, so the caller has to say so; the
    # event records that it was asserted rather than shown.
    event = runtime.remember(
        "Marc adores the city of Porto lately.", occurred_at=T1,
        entity="marc", attribute="likes", value="loves Porto",
        evidence_span="adores the city of Porto", paraphrased_value=True,
    )

    assert event.payload["self_supported"] is True
    assert event.payload["value_paraphrased"] is True
    assert event.payload["evidence_span"] == "adores the city of Porto"


def test_remember_rejects_a_paraphrase_that_does_not_declare_itself(runtime):
    # The same write without the flag must fail rather than silently record
    # an unverifiable paraphrase as if it were grounded.
    with pytest.raises(SchemaError) as exc_info:
        runtime.remember(
            "Marc adores the city of Porto lately.", occurred_at=T1,
            entity="marc", attribute="likes", value="loves Porto",
            evidence_span="adores the city of Porto",
        )
    assert "VALUE_NOT_IN_SPAN" in str(exc_info.value)


def test_remember_rejects_an_evidence_span_that_contradicts_the_value(runtime):
    # The span is real and appears verbatim in text, so SPAN_NOT_IN_SOURCE
    # does not catch it -- but it does not support `value`, it contradicts
    # it. Supplying a span used to set self_supported=True unconditionally,
    # which made recall() serve verified=True with a citation excerpt
    # ("Anna hates Porto") flatly contradicting its own claim.
    with pytest.raises(SchemaError) as exc_info:
        runtime.remember(
            "Anna hates Porto and refuses to move there.", occurred_at=T1,
            entity="anna", attribute="feeling_about_porto", value="loves Porto",
            evidence_span="Anna hates Porto",
        )
    assert "VALUE_NOT_IN_SPAN" in str(exc_info.value)


def test_remember_keeps_a_literally_grounded_span_unflagged(runtime):
    # The value appears inside its own quote -- that IS checkable, it
    # checks out, and nothing needs to be taken on the caller's word.
    event = runtime.remember(
        "Kim lives in Oslo these days.", occurred_at=T1,
        entity="kim", attribute="city", value="Oslo",
        evidence_span="Kim lives in Oslo",
    )

    assert event.payload["self_supported"] is True
    assert "value_paraphrased" not in event.payload


def test_remember_rejects_a_fabricated_evidence_span_at_write_time(runtime):
    with pytest.raises(SchemaError) as exc_info:
        runtime.remember(
            "Kim lives in Oslo", occurred_at=T1,
            entity="kim", attribute="city", value="Oslo",
            evidence_span="TOTAL FABRICATION AND APPEARS NOWHERE",
        )
    assert "SPAN_NOT_IN_SOURCE" in str(exc_info.value)


def test_remember_evidence_span_tolerates_whitespace_differences(runtime):
    event = runtime.remember(
        "Kim   lives\nin Oslo", occurred_at=T1,
        entity="kim", attribute="city", value="Oslo",
        evidence_span="Kim lives in Oslo",
    )

    assert event.payload["self_supported"] is True


def test_entity_detail_composes_a_distinct_chain_key(runtime):
    event = runtime.remember(
        "anna (coworker) moved to seattle", occurred_at=T1,
        entity="anna", attribute="city", value="Seattle", entity_detail="coworker at Acme",
    )

    assert event.payload["entity"] == "anna#coworker at Acme"
    assert event.payload["entity_label"] == "anna"
    assert event.payload["entity_detail"] == "coworker at Acme"


def test_remember_without_entity_detail_is_unaffected(runtime):
    event = runtime.remember("plan is pro", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    assert event.payload["entity"] == "user:1"
    assert "entity_label" not in event.payload
    assert "entity_detail" not in event.payload


@pytest.fixture
def schema_runtime(tmp_path):
    # A separate fixture, not a variant of `runtime` above: schema-on-write
    # is opt-in (config.schema_.known_types empty = off), and the bulk of
    # this suite (and test_server_tools.py) relies on that default staying
    # unrestricted -- these tests are the ones that deliberately turn it on.
    ws = Workspace(tmp_path / "schemaproject")
    config = OrlogConfig(
        workspace=WorkspaceConfig(name="schemaproject"),
        retrieval=RetrievalConfig(embedder="hashing"),
        schema_=SchemaConfig(known_types=["user"]),
    )
    rt = Runtime(ws, config)
    yield rt
    rt.close()


def test_default_config_leaves_schema_on_write_off(runtime):
    # known_types is empty by default -- any type/attribute string must be
    # accepted exactly as before this feature existed.
    event = runtime.remember("x", occurred_at=T1, entity="contact:1", attribute="anything", value="v")
    assert event.payload["entity"] == "contact:1"


def test_unknown_entity_type_is_rejected_when_schema_is_configured(schema_runtime):
    with pytest.raises(SchemaError) as exc_info:
        schema_runtime.remember("x", occurred_at=T1, entity="contact:1", attribute="email", value="v")
    assert "contact" in str(exc_info.value)
    assert "user" in str(exc_info.value)  # names the known set


def test_register_new_type_registers_it_and_its_first_attribute(schema_runtime):
    event = schema_runtime.remember(
        "x", occurred_at=T1, entity="contact:1", attribute="email", value="v", register_new_type=True,
    )
    assert event.payload["entity"] == "contact:1"

    # The type is now known, but a DIFFERENT attribute on it still isn't --
    # register_new_type doesn't blanket-authorize every future attribute.
    with pytest.raises(SchemaError) as exc_info:
        schema_runtime.remember("y", occurred_at=T1, entity="contact:2", attribute="phone", value="v2")
    assert "phone" in str(exc_info.value)
    assert "email" in str(exc_info.value)  # names the now-known attribute


def test_unknown_attribute_on_a_known_type_is_rejected(schema_runtime):
    schema_runtime.remember("x", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    with pytest.raises(SchemaError) as exc_info:
        schema_runtime.remember("y", occurred_at=T1, entity="user:2", attribute="favorite_brw", value="stout")
    assert "favorite_brw" in str(exc_info.value)
    assert "plan" in str(exc_info.value)  # names the known attribute(s) for "user"


def test_register_new_attribute_succeeds_explicitly(schema_runtime):
    schema_runtime.remember("x", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    event = schema_runtime.remember(
        "y", occurred_at=T1, entity="user:2", attribute="theme", value="dark", register_new_attribute=True,
    )
    assert event.payload["attribute"] == "theme"

    # theme is now known for "user" -- a third write can use it without the flag.
    event2 = schema_runtime.remember("z", occurred_at=T1, entity="user:3", attribute="theme", value="light")
    assert event2.payload["attribute"] == "theme"


def test_untyped_entity_is_exempt_from_schema_on_write(schema_runtime):
    # No ":" in "anna" -- schema-on-write has no type prefix to check.
    event = schema_runtime.remember("x", occurred_at=T1, entity="anna", attribute="anything", value="v")
    assert event.payload["entity"] == "anna"


def test_entity_detail_is_required_once_a_label_has_been_disambiguated(runtime):
    runtime.remember(
        "anna (sister) lives in Denver", occurred_at=T1,
        entity="anna", attribute="city", value="Denver", entity_detail="my sister",
    )

    with pytest.raises(SchemaError) as exc_info:
        runtime.remember("anna is somewhere else", occurred_at=T1, entity="anna", attribute="job", value="teacher")
    assert "my sister" in str(exc_info.value)


def test_entity_detail_matching_an_existing_one_continues_that_entity(runtime):
    runtime.remember(
        "anna (sister) lives in Denver", occurred_at=T1,
        entity="anna", attribute="city", value="Denver", entity_detail="my sister",
    )

    event = runtime.remember(
        "anna (sister) is a teacher", occurred_at=T1,
        entity="anna", attribute="job", value="teacher", entity_detail="my sister",
    )
    assert event.payload["entity"] == "anna#my sister"


def test_entity_detail_new_and_different_introduces_another_distinct_entity(runtime):
    runtime.remember(
        "anna (sister) lives in Denver", occurred_at=T1,
        entity="anna", attribute="city", value="Denver", entity_detail="my sister",
    )

    event = runtime.remember(
        "anna (coworker) lives in Seattle", occurred_at=T1,
        entity="anna", attribute="city", value="Seattle", entity_detail="coworker at Acme",
    )
    assert event.payload["entity"] == "anna#coworker at Acme"


def test_a_workspace_that_never_uses_entity_detail_is_unaffected(runtime):
    # Regression: repeated writes to the same never-disambiguated bare label
    # (test_server_tools.py's "which Anna" setup does exactly this) must
    # keep working -- existing_details is empty forever if entity_detail is
    # never supplied for that label.
    runtime.remember("anna lives in boston", occurred_at=T1, entity="anna", attribute="city", value="Boston")
    event = runtime.remember("anna moved to seattle", occurred_at=T1, entity="anna", attribute="city", value="Seattle")
    assert event.payload["entity"] == "anna"


def test_embedder_build_times_out_instead_of_hanging(tmp_path, monkeypatch):
    # Simulates a cold, network-bound embedder build that never finishes
    # (e.g. a stalled model download) -- the property must raise within
    # roughly the configured timeout, never hang indefinitely.
    import orlog.runtime as runtime_module

    def _hangs_forever(config):
        time.sleep(60)

    monkeypatch.setattr(runtime_module, "_build_embedder", _hangs_forever)

    ws = Workspace(tmp_path / "slowproject")
    config = OrlogConfig(
        workspace=WorkspaceConfig(name="slowproject"),
        retrieval=RetrievalConfig(embedder_build_timeout_s=0.2),
    )
    rt = Runtime(ws, config)

    start = time.monotonic()
    with pytest.raises(RetrieverUnavailableError):
        rt.embedder
    elapsed = time.monotonic() - start

    assert elapsed < 5  # bounded by the timeout, not the 60s fake hang
    rt.close()
