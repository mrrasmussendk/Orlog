"""Contract: each MCP tool function (ORLOG-SPEC.md §B9) works against a
real Runtime, with no `mcp` SDK involved -- server.py's FastMCP wiring is
a thin, separately-untested layer on top of these.
"""

import time
from datetime import datetime, timezone

import pytest

from orlog.config import OrlogConfig, RetrievalConfig, SchemaConfig, WorkspaceConfig
from orlog.errors import SchemaError
from orlog.retrieval_hybrid import KeyCandidate
from orlog.runtime import Runtime
from orlog.server_tools import (
    check_action_tool,
    list_attributes_tool,
    list_entities_tool,
    recall_history_tool,
    recall_tool,
    remember_tool,
    stats_tool,
)
from orlog.vault import generate_key
from orlog.workspace import Workspace

T1 = "2026-01-01T00:00:00+00:00"
T2 = "2026-06-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    monkeypatch.setenv("ORLOG_VAULT_KEY", generate_key())


@pytest.fixture
def runtime(tmp_path):
    # Pinned to "hashing" rather than relying on OrlogConfig's default: the
    # real default (spec §B3's fastembed/bge-small) is network-bound on a
    # cold build, which this Runtime-level test suite avoids (see README's
    # "Known deviations" -- test_retrieval_hybrid.py does exercise the real
    # FastEmbedEmbedder directly, just not through a full Runtime).
    # min_confidence/ambiguity_margin are relaxed back to the values
    # calibrated for "hashing" (config.py's own docstring) -- the global
    # defaults are now calibrated for bge-small.
    config = OrlogConfig(
        workspace=WorkspaceConfig(name="myproject"),
        retrieval=RetrievalConfig(embedder="hashing", min_confidence=0.15, ambiguity_margin=0.85),
    )
    rt = Runtime(Workspace(tmp_path / "myproject"), config)
    yield rt
    rt.close()


def test_remember_tool_returns_an_event_id_and_scrubs_pii(runtime):
    result = remember_tool(runtime, "email is alice@example.com", occurred_at=T1, entity="user:1", attribute="email", value="alice@example.com")

    assert "event_id" in result
    event = runtime.log.get(result["event_id"])
    assert "alice@example.com" not in event.payload["text"]


def test_remember_tool_treats_an_offset_less_occurred_at_as_utc(runtime):
    result = remember_tool(runtime, "plan is pro", occurred_at="2026-01-01T00:00:00", entity="user:1", attribute="plan", value="pro")

    event = runtime.log.get(result["event_id"])
    assert event.occurred_at == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_remember_tool_accepts_a_z_suffixed_occurred_at(runtime):
    result = remember_tool(runtime, "plan is pro", occurred_at="2026-01-01T00:00:00Z", entity="user:1", attribute="plan", value="pro")

    event = runtime.log.get(result["event_id"])
    assert event.occurred_at == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_recall_tool_treats_an_offset_less_as_of_as_utc(runtime):
    remember_tool(runtime, "plan is pro", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    result = recall_tool(runtime, "user:1.plan", as_of="2026-06-01T00:00:00")

    assert result["verified"] is True
    assert result["claim"] == "user:1.plan = pro"


def test_recall_tool_returns_a_verified_answer(runtime):
    remember_tool(runtime, "plan is pro", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    result = recall_tool(runtime, "user:1.plan", as_of=T2)

    assert result["verified"] is True
    assert result["claim"] == "user:1.plan = pro"


def test_recall_tool_fails_fast_on_a_self_contradictory_fact(runtime):
    # The fact's own remembered text ("moved to Paris") does not support the
    # value it was recorded with ("Tokyo") -- a caller-contradicted write,
    # not a projection bug. This must fail fast and stay failed on every
    # future read: no derive() round trip is ever attempted for it, so it
    # can never hang (see pipeline.py's self-consistency pre-check).
    remember_tool(
        runtime, "person trap moved to Paris", occurred_at=T1,
        entity="person:trap", attribute="city", value="Tokyo",
    )

    start = time.monotonic()
    result = recall_tool(runtime, "person:trap.city", as_of=T2)
    elapsed = time.monotonic() - start

    assert elapsed < 5  # never blocks on a contradiction -- no derive() attempted at all
    assert result["verified"] is False
    assert result["abstained"] is True
    assert result["reasons"] == ["UNSUPPORTED_BY_SOURCE"]
    assert stats_tool(runtime)["verifications"]["fail"] == {"UNSUPPORTED_BY_SOURCE": 1}

    # And it stays that way on a second read -- permanently poisoned, not a
    # one-off flake, with no derive() attempt (and thus no possible hang) on
    # this or any future call either.
    result_again = recall_tool(runtime, "person:trap.city", as_of=T2)
    assert result_again["verified"] is False
    assert result_again["reasons"] == ["UNSUPPORTED_BY_SOURCE"]


def test_recall_tool_passes_and_cites_the_evidence_span_for_a_paraphrased_value(runtime):
    # value ("loves Porto") never appears verbatim in text -- only the
    # separately-supplied evidence_span does. The citation excerpt must be
    # the span, not the value, since the span is what's actually grounded.
    remember_tool(
        runtime, "Marc adores the city of Porto lately.", occurred_at=T1,
        entity="marc", attribute="likes", value="loves Porto",
        evidence_span="adores the city of Porto", paraphrased_value=True,
    )

    result = recall_tool(runtime, "marc.likes", as_of=T2)

    assert result["verified"] is True
    assert result["claim"] == "marc.likes = loves Porto"
    assert result["citations"][0]["excerpt"] == "adores the city of Porto"


def test_remember_tool_rejects_a_fabricated_evidence_span(runtime):
    with pytest.raises(SchemaError) as exc_info:
        remember_tool(
            runtime, "Kim lives in Oslo", occurred_at=T1,
            entity="kim", attribute="city", value="Oslo",
            evidence_span="TOTAL FABRICATION AND APPEARS NOWHERE",
        )
    assert "SPAN_NOT_IN_SOURCE" in str(exc_info.value)


def test_recall_tool_abstains_with_no_facts_at_all(runtime):
    result = recall_tool(runtime, "user:1.plan", as_of=T2)
    assert result["abstained"] is True
    assert result["reasons"] == ["NO_CANDIDATES"]
    assert stats_tool(runtime)["recalls"] == 1
    assert stats_tool(runtime)["abstentions"]["by_reason"] == {"NO_CANDIDATES": 1}
    outcome_events = [e for e in runtime.log.read_all() if e.type == "outcome.abstention"]
    assert len(outcome_events) == 1
    assert outcome_events[0].payload == {"query_id": "user:1.plan", "reasons": ["NO_CANDIDATES"]}


def test_recall_tool_abstains_on_a_malformed_query(runtime):
    remember_tool(runtime, "plan is pro", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    result = recall_tool(runtime, "not-a-dotted-query", as_of=T2)

    assert result["abstained"] is True
    assert result["reasons"] == ["NO_CANDIDATES"]
    assert stats_tool(runtime)["recalls"] == 1
    assert stats_tool(runtime)["abstentions"]["by_reason"] == {"NO_CANDIDATES": 1}
    outcome_events = [e for e in runtime.log.read_all() if e.type == "outcome.abstention"]
    assert len(outcome_events) == 1


def test_recall_tool_accepts_a_z_suffixed_as_of(runtime):
    remember_tool(runtime, "plan is pro", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    result = recall_tool(runtime, "user:1.plan", as_of="2026-06-01T00:00:00Z")

    assert result["verified"] is True
    assert result["claim"] == "user:1.plan = pro"


def test_recall_history_tool_returns_the_full_chain(runtime):
    remember_tool(runtime, "plan is free", occurred_at=T1, entity="user:1", attribute="plan", value="free")
    remember_tool(runtime, "plan is pro", occurred_at=T2, entity="user:1", attribute="plan", value="pro")

    result = recall_history_tool(runtime, "user:1.plan")

    assert [entry["value"] for entry in result["chain"]] == ["free", "pro"]
    assert result["chain"][0]["valid_to"] == T2
    assert result["chain"][1]["valid_to"] is None  # still open-ended


def test_check_action_tool_warns_after_enough_prior_failures(runtime):
    # Some fact data must exist so build_pipeline() succeeds and
    # pipeline.answer() itself runs (and records the abstention via skuld)
    # -- querying a DIFFERENT, nonexistent entity still abstains with
    # NO_CANDIDATES, but this time through the real ledger-recording path.
    remember_tool(runtime, "plan is pro", occurred_at=T1, entity="user:1", attribute="plan", value="pro")
    for _ in range(3):
        recall_tool(runtime, "user:2.plan", as_of=T2)  # abstains each time, query_id defaults to the query itself

    result = check_action_tool(runtime, "user:2.plan")

    assert result["prior_outcomes"]["failures"] == 3
    assert result["warnings"]


def test_stats_tool_reflects_runtime_activity(runtime):
    remember_tool(runtime, "plan is pro", occurred_at=T1, entity="user:1", attribute="plan", value="pro")
    recall_tool(runtime, "user:1.plan", as_of=T2)

    snapshot = stats_tool(runtime)

    assert snapshot["appends"] == 1
    assert snapshot["recalls"] == 1


# -- free-text recall fallback (no "." in the query) --


def test_recall_tool_rescues_a_fact_found_only_by_meaning(runtime):
    remember_tool(
        runtime, "Anna just moved to Seattle last week", occurred_at=T1,
        entity="anna", attribute="city", value="Seattle",
    )

    result = recall_tool(runtime, "where does anna live now", as_of=T2)

    assert result["verified"] is True
    assert result["claim"] == "anna.city = Seattle"
    assert result["candidates"] == []


def test_recall_tool_free_text_still_returns_the_freshest_value_not_the_matched_mention(runtime):
    # The query text lexically resembles the OLD ("Boston") mention more
    # than the new one, but resolve_key() only has to find the KEY --
    # Pipeline.answer() is what guarantees the CURRENT value comes back.
    remember_tool(runtime, "Anna lives in Boston", occurred_at=T1, entity="anna", attribute="city", value="Boston")
    remember_tool(runtime, "Anna moved to Seattle", occurred_at=T2, entity="anna", attribute="city", value="Seattle")

    result = recall_tool(runtime, "anna lives in boston", as_of="2026-12-01T00:00:00Z")

    assert result["verified"] is True
    assert result["claim"] == "anna.city = Seattle"


def test_recall_tool_free_text_with_no_match_still_abstains_no_candidates(runtime):
    remember_tool(runtime, "plan is pro", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    result = recall_tool(runtime, "completely unrelated gibberish query", as_of=T2)

    assert result["abstained"] is True
    assert result["reasons"] == ["NO_CANDIDATES"]
    assert stats_tool(runtime)["recalls"] == 1


def test_recall_tool_dotted_query_for_a_nonexistent_entity_never_falls_back(runtime):
    # Regression guard: "plan" is a shared attribute name -- a fuzzy
    # fallback here would risk silently answering about the WRONG entity.
    remember_tool(runtime, "plan is pro", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    result = recall_tool(runtime, "user:2.plan", as_of=T2)

    assert result["abstained"] is True
    assert result["reasons"] == ["NO_CANDIDATES"]


# -- entity_detail disambiguation ("which Anna") --


def test_recall_tool_resolves_decisively_among_two_same_named_entities(runtime):
    remember_tool(
        runtime, "Anna the coworker moved to Seattle", occurred_at=T1,
        entity="anna", attribute="city", value="Seattle", entity_detail="coworker at Acme",
    )
    remember_tool(
        runtime, "Anna the roommate moved to Chicago", occurred_at=T1,
        entity="anna", attribute="city", value="Chicago", entity_detail="college roommate",
    )

    result = recall_tool(runtime, "where does anna the coworker at acme live", as_of=T2)

    assert result["verified"] is True
    assert result["claim"] == "anna#coworker at Acme.city = Seattle"


def test_remember_tool_requires_entity_detail_once_a_label_is_disambiguated(runtime):
    from orlog.errors import SchemaError

    remember_tool(
        runtime, "Anna the coworker moved to Seattle", occurred_at=T1,
        entity="anna", attribute="city", value="Seattle", entity_detail="coworker at Acme",
    )

    with pytest.raises(SchemaError) as exc_info:
        remember_tool(runtime, "Anna's job", occurred_at=T1, entity="anna", attribute="job", value="teacher")
    assert "coworker at Acme" in str(exc_info.value)


def test_remember_tool_enforces_schema_on_write_and_supports_registration(tmp_path):
    from orlog.config import SchemaConfig
    from orlog.errors import SchemaError

    config = OrlogConfig(
        workspace=WorkspaceConfig(name="schemaproject"),
        retrieval=RetrievalConfig(embedder="hashing"),
        schema_=SchemaConfig(known_types=["user"]),
    )
    rt = Runtime(Workspace(tmp_path / "schemaproject"), config)

    with pytest.raises(SchemaError):
        remember_tool(rt, "x", occurred_at=T1, entity="contact:1", attribute="email", value="v")

    event = remember_tool(rt, "x", occurred_at=T1, entity="contact:1", attribute="email", value="v", register_new_type=True)
    assert event["event_id"]
    rt.close()


def test_recall_tool_free_text_abstains_instead_of_hanging_when_embedder_is_unavailable(tmp_path, monkeypatch):
    # A cold/network-bound embedder build that never finishes (e.g. a
    # stalled model download) must never turn recall() into a multi-minute
    # hang -- it should abstain honestly, bounded by the configured timeout.
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
    remember_tool(rt, "plan is pro", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    start = time.monotonic()
    result = recall_tool(rt, "what plan does user 1 have", as_of=T2)
    elapsed = time.monotonic() - start

    assert elapsed < 5  # bounded by the timeout, not the 60s fake hang
    assert result["abstained"] is True
    assert result["reasons"] == ["EMBEDDER_UNAVAILABLE"]
    assert stats_tool(rt)["recalls"] == 1
    rt.close()


def test_recall_tool_reports_ambiguous_candidates_instead_of_guessing(runtime):
    remember_tool(
        runtime, "Anna is a coworker who lives somewhere", occurred_at=T1,
        entity="anna", attribute="city", value="Seattle", entity_detail="coworker at Acme",
    )
    remember_tool(
        runtime, "Anna is a coworker who lives somewhere", occurred_at=T1,
        entity="anna", attribute="city", value="Chicago", entity_detail="college roommate",
    )

    result = recall_tool(runtime, "where does anna live", as_of=T2)

    assert result["abstained"] is True
    assert result["verified"] is False
    assert result["claim"] is None
    assert result["reasons"] == ["AMBIGUOUS"]
    assert len(result["candidates"]) == 2
    details = {c["entity_detail"] for c in result["candidates"]}
    assert details == {"coworker at Acme", "college roommate"}
    assert stats_tool(runtime)["recalls"] == 1


# -- list_entities / list_attributes (the scan/list primitive) --


def test_list_attributes_returns_current_values_without_touching_the_embedder(runtime):
    remember_tool(runtime, "Emma lives in Oslo", occurred_at=T1, entity="person:emma", attribute="city", value="Oslo")
    remember_tool(runtime, "Emma is allergic to shellfish", occurred_at=T1, entity="person:emma", attribute="allergy", value="shellfish")
    remember_tool(runtime, "Emma moved to Berlin", occurred_at=T2, entity="person:emma", attribute="city", value="Berlin")

    result = list_attributes_tool(runtime, "person:emma")

    assert result["entity"] == "person:emma"
    by_attribute = {a["attribute"]: a for a in result["attributes"]}
    assert by_attribute["city"]["value"] == "Berlin"  # current, not the stale Oslo
    assert by_attribute["city"]["valid_to"] is None  # open-ended
    assert by_attribute["allergy"]["value"] == "shellfish"
    # zero-token, no-embedding guarantee: no free-text recall was involved,
    # so Runtime.embedder must never have been built.
    assert runtime._embedder is None


def test_list_attributes_on_an_unknown_entity_returns_empty(runtime):
    result = list_attributes_tool(runtime, "person:nonexistent")
    assert result == {"entity": "person:nonexistent", "attributes": []}


def test_list_attributes_on_an_ambiguous_bare_label_reports_candidates(runtime):
    remember_tool(
        runtime, "Anna the coworker moved to Seattle", occurred_at=T1,
        entity="anna", attribute="city", value="Seattle", entity_detail="coworker at Acme",
    )
    remember_tool(
        runtime, "Anna the roommate moved to Chicago", occurred_at=T1,
        entity="anna", attribute="city", value="Chicago", entity_detail="college roommate",
    )

    result = list_attributes_tool(runtime, "anna")

    assert result["ambiguous"] is True
    assert result["entity"] == "anna"
    details = {c["entity_detail"] for c in result["candidates"]}
    assert details == {"coworker at Acme", "college roommate"}


def test_list_attributes_resolves_the_exact_labelhash_detail_form(runtime):
    remember_tool(
        runtime, "Anna the coworker moved to Seattle", occurred_at=T1,
        entity="anna", attribute="city", value="Seattle", entity_detail="coworker at Acme",
    )

    result = list_attributes_tool(runtime, "anna#coworker at Acme")

    assert result["entity"] == "anna#coworker at Acme"
    assert result["attributes"][0]["value"] == "Seattle"


def test_list_entities_groups_by_label_and_detail_with_attribute_counts(runtime):
    remember_tool(runtime, "Emma lives in Oslo", occurred_at=T1, entity="person:emma", attribute="city", value="Oslo")
    remember_tool(runtime, "Emma is allergic to shellfish", occurred_at=T1, entity="person:emma", attribute="allergy", value="shellfish")
    remember_tool(runtime, "plan is pro", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    result = list_entities_tool(runtime)

    by_label = {e["entity_label"]: e for e in result["entities"]}
    assert by_label["person:emma"]["attribute_count"] == 2
    assert by_label["person:emma"]["entity_detail"] is None
    assert by_label["user:1"]["attribute_count"] == 1
    assert runtime._embedder is None


def test_list_entities_prefix_and_limit_filter_and_truncate(runtime):
    remember_tool(runtime, "x", occurred_at=T1, entity="person:emma", attribute="city", value="Oslo")
    remember_tool(runtime, "x", occurred_at=T1, entity="person:trap", attribute="city", value="Tokyo")
    remember_tool(runtime, "x", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    prefixed = list_entities_tool(runtime, prefix="person:")
    assert {e["entity_label"] for e in prefixed["entities"]} == {"person:emma", "person:trap"}

    limited = list_entities_tool(runtime, limit=1)
    assert len(limited["entities"]) == 1


def test_entity_detail_on_a_first_not_yet_disambiguated_write_is_accepted(runtime):
    result = remember_tool(
        runtime, "Anna the coworker moved to Seattle", occurred_at=T1,
        entity="anna", attribute="city", value="Seattle", entity_detail="coworker at Acme",
    )
    assert "event_id" in result
    answer = recall_tool(runtime, "anna#coworker at Acme.city", as_of=T2)
    assert answer["verified"] is True
    assert answer["claim"] == "anna#coworker at Acme.city = Seattle"


def test_register_new_type_and_register_new_attribute_together_for_a_brand_new_type(tmp_path):
    config = OrlogConfig(
        workspace=WorkspaceConfig(name="schemaproject2"),
        retrieval=RetrievalConfig(embedder="hashing"),
        schema_=SchemaConfig(known_types=["user"]),
    )
    rt = Runtime(Workspace(tmp_path / "schemaproject2"), config)
    result = remember_tool(
        rt, "x", occurred_at=T1, entity="contact:1", attribute="email", value="v",
        register_new_type=True, register_new_attribute=True,
    )
    assert "event_id" in result
    rt.close()


def test_register_new_attribute_for_an_already_known_type(tmp_path):
    from orlog.errors import SchemaError

    config = OrlogConfig(
        workspace=WorkspaceConfig(name="schemaproject3"),
        retrieval=RetrievalConfig(embedder="hashing"),
        schema_=SchemaConfig(known_types=["user"]),
    )
    rt = Runtime(Workspace(tmp_path / "schemaproject3"), config)
    remember_tool(rt, "x", occurred_at=T1, entity="user:1", attribute="plan", value="pro")

    with pytest.raises(SchemaError):
        remember_tool(rt, "x", occurred_at=T1, entity="user:2", attribute="shoe_size", value="42")

    result = remember_tool(rt, "x", occurred_at=T1, entity="user:2", attribute="shoe_size", value="42", register_new_attribute=True)
    assert "event_id" in result
    rt.close()


def test_invalid_occurred_at_string_raises_a_clear_schema_error(runtime):
    # E_SCHEMA, the same taxonomy every other bad-input rejection in
    # remember() uses -- not a bare ValueError. server.py's header promises
    # tool errors map to abstentions or the closed taxonomy, and a raw
    # ValueError was in neither.
    with pytest.raises(SchemaError):
        remember_tool(runtime, text="x", occurred_at="not-a-date", entity="e", attribute="a", value="v")


def test_invalid_as_of_string_raises_a_clear_schema_error(runtime):
    # "yesterday" is a realistic thing for an LLM caller to send for a
    # parameter documented as "the instant you are asking about"; it used
    # to escape as a raw ValueError from datetime.fromisoformat.
    for bad in ("yesterday", "", "2026-13-99"):
        with pytest.raises(SchemaError):
            recall_tool(runtime, "user:1.plan", as_of=bad)

    with pytest.raises(SchemaError):
        recall_tool(runtime, "user:1.plan", known_as_of="not-a-date")


def test_recall_query_with_more_than_one_dot_splits_on_the_last_one(runtime):
    remember_tool(runtime, "dark theme", occurred_at=T1, entity="user:1.settings", attribute="theme", value="dark")

    result = recall_tool(runtime, "user:1.settings.theme", as_of=T2)

    assert result["verified"] is True
    assert result["claim"] == "user:1.settings.theme = dark"


def test_recall_is_case_sensitive_on_the_exact_key_lookup_path(runtime):
    remember_tool(runtime, "plan is pro", occurred_at=T1, entity="User:1", attribute="Plan", value="pro")

    exact_case = recall_tool(runtime, "User:1.Plan", as_of=T2)
    different_case = recall_tool(runtime, "user:1.plan", as_of=T2)

    assert exact_case["verified"] is True
    assert different_case["abstained"] is True
    assert different_case["reasons"] == ["NO_CANDIDATES"]


def test_recall_tool_ambiguity_margin_boundary_is_inclusive(runtime, monkeypatch):
    remember_tool(runtime, "seed fact", occurred_at=T1, entity="seed", attribute="x", value="y")
    import orlog.server_tools as server_tools_module

    margin = runtime.config.retrieval.ambiguity_margin
    candidates = [
        KeyCandidate(entity="a", attribute="attr", event_id="ev-a", matched_text="a", score=1.0),
        KeyCandidate(entity="b", attribute="attr", event_id="ev-b", matched_text="b", score=1.0 * margin),
    ]
    monkeypatch.setattr(server_tools_module, "resolve_key", lambda *a, **k: candidates)

    result = recall_tool(runtime, "free text query", as_of=T2)

    assert result["reasons"] == ["AMBIGUOUS"]
    assert len(result["candidates"]) == 2


def test_recall_tool_ambiguity_margin_boundary_excludes_a_runner_up_just_below_it(runtime, monkeypatch):
    remember_tool(runtime, "seed fact", occurred_at=T1, entity="seed", attribute="x", value="y")
    import orlog.server_tools as server_tools_module

    margin = runtime.config.retrieval.ambiguity_margin
    candidates = [
        KeyCandidate(entity="a", attribute="attr", event_id="ev-a", matched_text="a", score=1.0),
        KeyCandidate(entity="b", attribute="attr", event_id="ev-b", matched_text="b", score=1.0 * margin - 0.05),
    ]
    monkeypatch.setattr(server_tools_module, "resolve_key", lambda *a, **k: candidates)

    result = recall_tool(runtime, "free text query", as_of=T2)

    assert result["reasons"] != ["AMBIGUOUS"]
