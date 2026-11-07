"""Contract: the read-only index query API (ORLOG-SPEC.md §B12.4).

The four scan tools used to read the whole log on every call. These assert
both halves of the migration: the index answers them, and it answers them
with exactly what the log says.
"""

from datetime import datetime, timedelta, timezone

import pytest

from orlog.config import OrlogConfig, RetrievalConfig, WorkspaceConfig
from orlog.models.event import EventDraft
from orlog.queries import events_match_log
from orlog.runtime import Runtime
from orlog.server_tools import (
    check_action_tool,
    list_attributes_tool,
    list_entities_tool,
    recall_history_tool,
)
from orlog.storage import EventIndex
from orlog.vault import generate_key
from orlog.workspace import Workspace

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    monkeypatch.setenv("ORLOG_VAULT_KEY", generate_key())


def _config(name="queries"):
    return OrlogConfig(
        workspace=WorkspaceConfig(name=name), retrieval=RetrievalConfig(embedder="hashing")
    )


@pytest.fixture
def runtime(tmp_path):
    runtime = Runtime(Workspace(tmp_path / "ws"), _config())
    yield runtime
    runtime.close()


def _fact(runtime, entity, attribute, value, *, day=0, detail=None):
    return runtime.remember(
        value,
        occurred_at=T0 + timedelta(days=day),
        entity=entity,
        attribute=attribute,
        value=value,
        entity_detail=detail,
    )


# -- the index is the log --------------------------------------------------


def test_the_index_returns_exactly_the_logs_events_in_order(runtime):
    for i in range(5):
        _fact(runtime, f"user:{i}", "plan", f"plan {i}", day=i)
    assert events_match_log(runtime.query, runtime.log.read_all())


def test_a_queried_event_is_byte_identical_to_the_logged_one(runtime):
    written = _fact(runtime, "user:1", "plan", "pro")
    (read_back,) = runtime.query.events()
    assert read_back == written
    assert read_back.prev_hash == written.prev_hash


def test_an_index_behind_the_log_is_rebuilt_at_startup(tmp_path):
    ws = Workspace(tmp_path / "stale")
    runtime = Runtime(ws, _config())
    _fact(runtime, "user:1", "plan", "pro")
    runtime.close()

    # Simulate a workspace written before these tables existed, or an index
    # that fell behind: drop every derived row and reopen.
    index = EventIndex(ws.index_path)
    index.connection.executescript("DELETE FROM events_idx; DELETE FROM event_bodies; DELETE FROM facts_idx;")
    index.connection.commit()
    index.close()

    reopened = Runtime(ws, _config())
    try:
        # A derived cache that disagrees with the log is repaired by
        # rebuilding it -- no migration command, no stale-read window.
        assert events_match_log(reopened.query, reopened.log.read_all())
    finally:
        reopened.close()


# -- filters ---------------------------------------------------------------


def test_events_filters_by_exact_type_and_by_namespace_prefix(runtime):
    _fact(runtime, "user:1", "plan", "pro")
    runtime.log.append(
        EventDraft(occurred_at=T0, actor="s", type="outcome.abstention", payload={"query_id": "q"})
    )
    runtime.index.index_appended(runtime.log, runtime.log.read_all()[-1])

    assert [e.type for e in runtime.query.events(types=["fact"])] == ["fact"]
    assert [e.type for e in runtime.query.events(type_prefix="outcome.")] == ["outcome.abstention"]
    assert runtime.query.events(types=[]) == []


def test_events_filters_by_valid_time_range_half_open(runtime):
    a = _fact(runtime, "user:1", "plan", "a", day=0)
    b = _fact(runtime, "user:1", "plan", "b", day=1)
    _fact(runtime, "user:1", "plan", "c", day=2)

    found = runtime.query.events(
        occurred_since=T0, occurred_until=T0 + timedelta(days=2)
    )
    assert [e.id for e in found] == [a.id, b.id]


def test_events_filters_by_transaction_time_horizon(runtime):
    old = runtime.remember(
        "old", occurred_at=T0, recorded_at=T0, entity="user:1", attribute="plan", value="old"
    )
    runtime.remember("new", occurred_at=T0, entity="user:1", attribute="plan", value="new")

    found = runtime.query.events(known_as_of=T0 + timedelta(days=1))
    assert [e.id for e in found] == [old.id]


def test_a_timestamp_written_with_a_non_utc_offset_still_falls_inside_its_horizon(runtime):
    """ISO-8601 only sorts lexicographically when every value shares one
    offset. A fact written at noon in Copenhagen is 10:00 UTC, and must be
    inside a 11:00 UTC horizon -- which a naive string compare gets wrong."""
    cph = timezone(timedelta(hours=2))
    event = runtime.remember(
        "noon in Copenhagen",
        occurred_at=datetime(2026, 6, 1, 12, 0, tzinfo=cph),
        recorded_at=datetime(2026, 6, 1, 12, 0, tzinfo=cph),
        entity="user:1",
        attribute="plan",
        value="noon in Copenhagen",
    )
    horizon = datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc)
    assert [e.id for e in runtime.query.events(known_as_of=horizon)] == [event.id]


def test_a_prefix_filter_is_case_sensitive_and_takes_wildcards_literally(runtime):
    _fact(runtime, "user_id:1", "plan", "a")
    _fact(runtime, "User:2", "plan", "b")

    # SQLite's LIKE would match "User:2" for prefix "user" and would read
    # the "_" in "user_id" as a wildcard. Neither is what a caller asked for.
    assert [s.entity_label for s in runtime.query.entity_summaries(prefix="user")] == ["user_id:1"]
    assert runtime.query.entity_summaries(prefix="user_") == runtime.query.entity_summaries(prefix="user_id")


# -- the four migrated tools ------------------------------------------------


def test_list_entities_groups_and_counts_through_the_index(runtime):
    _fact(runtime, "anna", "city", "porto", detail="my sister")
    _fact(runtime, "anna", "job", "vet", detail="my sister")
    _fact(runtime, "anna", "city", "lisbon", detail="coworker")
    _fact(runtime, "bob", "city", "oslo")

    assert list_entities_tool(runtime)["entities"] == [
        {"entity_label": "anna", "entity_detail": "coworker", "attribute_count": 1},
        {"entity_label": "anna", "entity_detail": "my sister", "attribute_count": 2},
        {"entity_label": "bob", "entity_detail": None, "attribute_count": 1},
    ]
    assert [e["entity_detail"] for e in list_entities_tool(runtime, prefix="a")["entities"]] == [
        "coworker",
        "my sister",
    ]
    assert len(list_entities_tool(runtime, limit=2)["entities"]) == 2


def test_list_attributes_still_abstains_on_an_ambiguous_bare_label(runtime):
    _fact(runtime, "anna", "city", "porto", detail="my sister")
    _fact(runtime, "anna", "city", "lisbon", detail="coworker")

    result = list_attributes_tool(runtime, "anna")
    assert result["ambiguous"] is True
    assert [c["entity"] for c in result["candidates"]] == ["anna#coworker", "anna#my sister"]


def test_list_attributes_answers_for_an_exact_disambiguated_key(runtime):
    _fact(runtime, "anna", "city", "porto", detail="my sister")
    _fact(runtime, "anna", "city", "lisbon", detail="coworker")

    result = list_attributes_tool(runtime, "anna#my sister")
    assert result["entity"] == "anna#my sister"
    assert [a["value"] for a in result["attributes"]] == ["porto"]


def test_recall_history_reads_one_key_not_the_whole_log(runtime):
    _fact(runtime, "user:1", "plan", "free", day=0)
    _fact(runtime, "user:1", "plan", "pro", day=1)
    _fact(runtime, "user:2", "plan", "noise", day=0)

    chain = recall_history_tool(runtime, "user:1.plan")["chain"]
    assert [c["value"] for c in chain] == ["free", "pro"]


def test_recall_history_respects_the_transaction_time_horizon(runtime):
    runtime.remember(
        "free", occurred_at=T0, recorded_at=T0, entity="user:1", attribute="plan", value="free"
    )
    runtime.remember(
        "pro", occurred_at=T0 + timedelta(days=1), entity="user:1", attribute="plan", value="pro"
    )

    chain = recall_history_tool(runtime, "user:1.plan", known_as_of=T0.isoformat())["chain"]
    assert [c["value"] for c in chain] == ["free"]


def test_check_action_counts_outcomes_through_the_index(runtime):
    for _ in range(3):
        runtime.ledger.record_abstention(query_id="deploy prod", reasons=["NO_CANDIDATES"], occurred_at=T0)

    result = check_action_tool(runtime, "deploy prod")
    assert result["prior_outcomes"]["failures"] == 3
    assert result["warnings"]


def test_check_action_substring_match_stays_case_sensitive(runtime):
    runtime.ledger.record_abstention(query_id="Deploy Prod", reasons=["X"], occurred_at=T0)
    assert check_action_tool(runtime, "deploy prod")["prior_outcomes"]["failures"] == 0
    assert check_action_tool(runtime, "Deploy")["prior_outcomes"]["failures"] == 1


# -- schema-on-write reads --------------------------------------------------


def test_entity_types_and_attributes_come_from_the_index(runtime):
    _fact(runtime, "person:emma", "city", "porto")
    _fact(runtime, "person:ola", "job", "vet")
    _fact(runtime, "bare", "thing", "x")

    assert runtime.query.entity_types_in_log() == {"person"}
    assert runtime.query.attributes_for_entity_type("person") == {"city", "job"}


def test_details_for_label_comes_from_the_index(runtime):
    _fact(runtime, "anna", "city", "porto", detail="my sister")
    assert runtime.query.details_for_label("anna") == {"my sister"}
    assert runtime.query.details_for_label("bob") == set()


def test_repeated_recalls_reuse_the_projection_instead_of_rebuilding_it(runtime):
    """build_pipeline() used to re-project the whole log on every recall().
    With the registry's cache it rebuilds once and then folds each append in
    -- including across the outcome events an abstention appends, which is
    the case that used to cost a full rebuild every time."""
    from orlog.server_tools import recall_tool

    _fact(runtime, "user:1", "plan", "pro")
    for _ in range(5):
        recall_tool(runtime, "user:1.plan")
        recall_tool(runtime, "user:404.plan")  # abstains, appending an outcome event

    store = runtime.projection_store
    assert store.misses == 1
    assert store.hits >= 9


def test_an_append_between_recalls_is_folded_into_the_cached_view(runtime):
    from orlog.server_tools import recall_tool

    _fact(runtime, "user:1", "plan", "free", day=0)
    recall_tool(runtime, "user:1.plan")
    _fact(runtime, "user:1", "plan", "pro", day=1)

    answer = recall_tool(runtime, "user:1.plan", as_of=(T0 + timedelta(days=2)).isoformat())

    assert runtime.projection_store.misses == 1
    assert runtime.projection_store.incremental >= 1
    assert "pro" in str(answer["claim"])


def test_stats_reports_the_projection_cache(runtime):
    from orlog.server_tools import recall_tool, stats_tool

    _fact(runtime, "user:1", "plan", "pro")
    recall_tool(runtime, "user:1.plan")
    recall_tool(runtime, "user:1.plan")

    projections = stats_tool(runtime)["projections"]
    assert projections["projection_cache_misses"] == 1
    assert projections["projection_cache_hits"] >= 1
    assert projections["projection_cache_entries"] == 1
