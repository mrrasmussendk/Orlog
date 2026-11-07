"""Contract: the projection registry, its cache and its rebuild path
(ORLOG-SPEC.md §B12.2).
"""

from datetime import datetime, timedelta, timezone

import pytest

from orlog.config import OrlogConfig, RetrievalConfig, WorkspaceConfig
from orlog.models.event import EventDraft
from orlog.plugins import PROJECTIONS_GROUP, PluginError, clear_registered, register
from orlog.projections import (
    ProjectionRegistry,
    ProjectionStore,
    SessionSummaries,
    SupersessionChains,
)
from orlog.runtime import Runtime
from orlog.vault import generate_key
from orlog.verdandi import ProjectionVersion
from orlog.workspace import Workspace

BUILT_AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class CountingProjection:
    """Counts how many times it is asked to build, so the test can tell a
    cache hit from a rebuild without timing anything."""

    name = "counting"
    version = 1

    def __init__(self) -> None:
        self.builds = 0

    def build(self, events, *, built_at):
        self.builds += 1
        return (
            {"ids": [e.id for e in events]},
            ProjectionVersion(
                projection=self.name,
                version=self.version,
                built_from=events[-1].id,
                built_at=built_at,
                builder="counting",
            ),
        )


class IncrementalProjection(CountingProjection):
    name = "incremental"

    def __init__(self) -> None:
        super().__init__()
        self.updates = 0

    def update(self, view, version, new_events, *, built_at):
        self.updates += 1
        ids = view["ids"] + [e.id for e in new_events]
        return (
            {"ids": ids},
            version.model_copy(update={"built_from": new_events[-1].id, "built_at": built_at}),
        )


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    monkeypatch.setenv("ORLOG_VAULT_KEY", generate_key())


@pytest.fixture(autouse=True)
def clean_registry():
    clear_registered()
    yield
    clear_registered()


@pytest.fixture
def runtime(tmp_path):
    runtime = Runtime(
        Workspace(tmp_path / "ws"),
        OrlogConfig(
            workspace=WorkspaceConfig(name="proj"), retrieval=RetrievalConfig(embedder="hashing")
        ),
    )
    yield runtime
    runtime.close()


def _facts(runtime, n, *, entity="user:1"):
    return [
        runtime.remember(
            f"value {i}",
            occurred_at=T0 + timedelta(days=i),
            entity=entity,
            attribute="plan",
            value=f"value {i}",
        )
        for i in range(n)
    ]


# -- registry --------------------------------------------------------------


def test_core_projections_are_registered_through_the_same_seam():
    registry = ProjectionRegistry.load()
    assert registry.names == ("supersession_chains", "session_summaries")
    assert isinstance(registry.get("supersession_chains"), SupersessionChains)
    assert isinstance(registry.get("session_summaries"), SessionSummaries)
    assert registry.distribution_of("supersession_chains") == "orlog"


def test_a_profile_cannot_replace_a_core_projection():
    register(PROJECTIONS_GROUP, "evil", {"supersession_chains": CountingProjection}, distribution="evil")
    with pytest.raises(PluginError, match="not overridable"):
        ProjectionRegistry.load()


def test_an_object_with_no_build_method_is_refused():
    register(PROJECTIONS_GROUP, "bad", {"bad": object()}, distribution="bad")
    with pytest.raises(PluginError, match="no build"):
        ProjectionRegistry.load()


def test_an_entry_point_name_must_match_the_projections_own_name():
    register(PROJECTIONS_GROUP, "mismatch", {"other_name": CountingProjection}, distribution="x")
    with pytest.raises(PluginError, match="calls itself"):
        ProjectionRegistry.load()


# -- cache and incremental update ------------------------------------------


def test_an_unchanged_source_set_is_served_from_cache(runtime):
    projection = CountingProjection()
    store = ProjectionStore(ProjectionRegistry([("counting", projection, "test")]))
    events = _facts(runtime, 3)

    first = store.view("counting", events, built_at=BUILT_AT)
    second = store.view("counting", events, built_at=BUILT_AT)

    assert projection.builds == 1
    assert first is second
    assert store.hits == 1


def test_an_append_extends_a_cached_view_through_update_when_there_is_one(runtime):
    projection = IncrementalProjection()
    store = ProjectionStore(ProjectionRegistry([("incremental", projection, "test")]))
    events = _facts(runtime, 3)
    store.view("incremental", events, built_at=BUILT_AT)

    events.append(
        runtime.remember(
            "value 3", occurred_at=T0 + timedelta(days=3), entity="user:1", attribute="plan", value="value 3"
        )
    )
    result = store.view("incremental", events, built_at=BUILT_AT)

    assert projection.builds == 1  # not rebuilt from scratch
    assert projection.updates == 1
    assert result.view["ids"] == [e.id for e in events]


def test_a_projection_without_update_is_rebuilt_over_the_widened_set(runtime):
    projection = CountingProjection()
    store = ProjectionStore(ProjectionRegistry([("counting", projection, "test")]))
    events = _facts(runtime, 2)
    store.view("counting", events, built_at=BUILT_AT)
    events.append(
        runtime.remember(
            "value 2", occurred_at=T0 + timedelta(days=2), entity="user:1", attribute="plan", value="value 2"
        )
    )

    result = store.view("counting", events, built_at=BUILT_AT)

    assert projection.builds == 2
    assert result.view["ids"] == [e.id for e in events]


def test_incremental_and_cold_builds_agree(runtime):
    """The optimization must not be a second answer."""
    events = _facts(runtime, 5)
    warm = ProjectionStore(ProjectionRegistry.load())
    for i in range(1, 6):
        warm_result = warm.view("supersession_chains", events[:i], built_at=BUILT_AT)
    cold = ProjectionStore(ProjectionRegistry.load())
    cold_result = cold.view("supersession_chains", events, built_at=BUILT_AT)

    assert warm_result.view == cold_result.view
    assert warm_result.version == cold_result.version


def test_a_different_source_history_is_not_served_a_cached_view(runtime):
    """Two source sets of the same length whose tails differ are different
    projections, and the cache must notice."""
    projection = CountingProjection()
    store = ProjectionStore(ProjectionRegistry([("counting", projection, "test")]))
    events = _facts(runtime, 3)
    store.view("counting", events, built_at=BUILT_AT)

    other = list(events[:2]) + [
        runtime.remember(
            "other", occurred_at=T0 + timedelta(days=9), entity="user:2", attribute="plan", value="other"
        )
    ]
    result = store.view("counting", other, built_at=BUILT_AT)

    assert projection.builds == 2
    assert result.view["ids"] == [e.id for e in other]


def test_a_pinned_horizon_gets_its_own_cache_entry(runtime):
    projection = CountingProjection()
    store = ProjectionStore(ProjectionRegistry([("counting", projection, "test")]))
    events = _facts(runtime, 3)
    store.view("counting", events, built_at=BUILT_AT)
    store.view("counting", events[:2], built_at=BUILT_AT, known_as_of=T0)

    assert projection.builds == 2
    # The unbounded view is untouched by the pinned one.
    assert store.view("counting", events, built_at=BUILT_AT).view["ids"] == [e.id for e in events]
    assert projection.builds == 2


def test_on_append_drops_horizon_pinned_views_rather_than_patching_them(runtime):
    projection = CountingProjection()
    store = ProjectionStore(ProjectionRegistry([("counting", projection, "test")]))
    events = _facts(runtime, 2)
    store.view("counting", events, built_at=BUILT_AT, known_as_of=T0 + timedelta(days=30))
    assert store._entries

    store.on_append(events[-1], built_at=BUILT_AT)

    # recorded_at is not monotonic, so a new event may belong in the MIDDLE
    # of a pinned view's source set. Dropping is the only correct move.
    assert store._entries == {}


def test_an_append_of_an_unselected_event_leaves_the_view_alone(runtime):
    store = ProjectionStore(ProjectionRegistry.load())
    events = _facts(runtime, 2)
    before = store.view("supersession_chains", events, built_at=BUILT_AT)

    unrelated = runtime.log.append(
        EventDraft(occurred_at=T0, actor="system", type="outcome.abstention", payload={"query_id": "q"})
    )
    store.on_append(unrelated, built_at=BUILT_AT)

    after = store.view("supersession_chains", events + [unrelated], built_at=BUILT_AT)
    assert after.view == before.view


# -- replay ----------------------------------------------------------------


def test_rebuild_all_rebuilds_every_registered_projection(runtime):
    register(PROJECTIONS_GROUP, "counting", {"counting": CountingProjection}, distribution="test")
    registry = ProjectionRegistry.load()
    store = ProjectionStore(registry, index=runtime.index)
    _facts(runtime, 2)
    runtime.log.append(
        EventDraft(occurred_at=T0, actor="u", type="fact", payload={"session_id": "s1", "text": "hi"})
    )

    built = store.rebuild_all(runtime.log.read_all(), built_at=BUILT_AT)

    assert set(built) == {"supersession_chains", "session_summaries", "counting"}
    rows = runtime.index.connection.execute(
        "SELECT projection, builder FROM projections ORDER BY projection"
    ).fetchall()
    assert dict(rows) == {
        "counting": "counting",
        "session_summaries": "orlog-runtime",
        "supersession_chains": "orlog-runtime",
    }


def test_a_projection_with_no_source_events_is_omitted_not_an_error(runtime):
    store = ProjectionStore(ProjectionRegistry.load(), index=runtime.index)
    _facts(runtime, 1)
    built = store.rebuild_all(runtime.log.read_all(), built_at=BUILT_AT)
    # No event carries a session_id, so session_summaries has nothing to
    # build from -- which is not a failure.
    assert set(built) == {"supersession_chains"}


def test_rebuild_all_drops_the_cache_first(runtime):
    projection = CountingProjection()
    store = ProjectionStore(ProjectionRegistry([("counting", projection, "test")]))
    events = _facts(runtime, 2)
    store.view("counting", events, built_at=BUILT_AT)

    store.rebuild_all(events, built_at=BUILT_AT)

    # `orlog replay` must prove the view comes from the log, not from
    # whatever this process happened to be holding.
    assert projection.builds == 2


# -- preselected sources ----------------------------------------------------


def test_a_preselected_entry_ignores_an_append_it_does_not_select(runtime):
    """Runtime hands the store only the fact events. An outcome event
    appended between two recalls must leave that cache entry alone --
    bumping its length for an event that will never appear in the caller's
    list made every later lookup miss, so the cache went cold the first
    time anything abstained."""
    store = ProjectionStore(ProjectionRegistry.load())
    facts = _facts(runtime, 2)
    store.view("supersession_chains", facts, built_at=BUILT_AT, preselected=True)

    outcome = runtime.log.append(
        EventDraft(occurred_at=T0, actor="system", type="outcome.abstention", payload={"query_id": "q"})
    )
    store.on_append(outcome, built_at=BUILT_AT)

    store.view("supersession_chains", facts, built_at=BUILT_AT, preselected=True)
    assert store.hits == 1


def test_a_whole_log_entry_absorbs_an_append_it_does_not_select(runtime):
    """`orlog replay` hands the store everything and lets selects() filter,
    so that entry's length DOES grow for an unselected append."""
    store = ProjectionStore(ProjectionRegistry.load())
    _facts(runtime, 2)
    store.view("supersession_chains", runtime.log.read_all(), built_at=BUILT_AT)

    outcome = runtime.log.append(
        EventDraft(occurred_at=T0, actor="system", type="outcome.abstention", payload={"query_id": "q"})
    )
    store.on_append(outcome, built_at=BUILT_AT)

    store.view("supersession_chains", runtime.log.read_all(), built_at=BUILT_AT)
    assert store.hits == 1


def test_a_preselected_call_is_never_served_a_whole_log_entry(runtime):
    """Two source sets that happen to agree on (length, last id) are still
    two different source sets if one was filtered and the other was not --
    the cache must not extend one from the other's base."""
    projection = CountingProjection()
    store = ProjectionStore(ProjectionRegistry([("counting", projection, "test")]))
    everything = runtime.log.read_all() + _facts(runtime, 2)
    store.view("counting", everything, built_at=BUILT_AT)

    result = store.view("counting", everything, built_at=BUILT_AT, preselected=True)

    assert projection.builds == 2
    assert result.view["ids"] == [e.id for e in everything]


def test_a_preselected_call_skips_the_selects_filter(runtime):
    """preselected is a promise, and the store takes it at face value --
    that is what keeps the cache check O(1) on the recall() path."""
    store = ProjectionStore(ProjectionRegistry.load())
    runtime.log.append(
        EventDraft(occurred_at=T0, actor="s", type="outcome.abstention", payload={"query_id": "q"})
    )
    facts = _facts(runtime, 1)
    mixed = runtime.log.read_all()

    filtered = store.view("supersession_chains", mixed, built_at=BUILT_AT)
    assert [e.id for e in filtered.events] == [facts[0].id]

    store.invalidate()
    # Told the list is already its source set, the store hands the outcome
    # event straight to the projection -- which keys no chain from it, so
    # the view is empty and built_from names that event. The filtered call
    # above never showed it to the projection at all. That difference is
    # exactly what `preselected` promises away, which is why only Runtime
    # (whose index query IS the filter) passes it.
    unfiltered = store.view(
        "supersession_chains", [mixed[0]], built_at=BUILT_AT, preselected=True
    )
    assert mixed[0].type == "outcome.abstention"
    assert unfiltered.view.chains == {}
    assert unfiltered.version.built_from == mixed[0].id
