"""Contract: the TRANSACTION-time axis (recorded_at / known_as_of).

`as_of` alone answers "what was true on day Y". It cannot answer "what did
this system BELIEVE on day X" -- because a correction appended later
supersedes on the valid-time axis and so silently rewrites the answer to a
question about the past. These tests pin the second axis that separates the
two, and the regression that motivated it.
"""

from datetime import datetime, timezone

import pytest

from orlog.config import OrlogConfig, RetrievalConfig, WorkspaceConfig
from orlog.errors import SchemaError
from orlog.runtime import Runtime
from orlog.server_tools import recall_history_tool, recall_tool, remember_tool
from orlog.vault import generate_key
from orlog.workspace import Workspace

# The scenario from the timetravel spec, in miniature: a fact is true from
# day 1, a WRONG value for it is recorded on day 3, and the correction only
# lands on day 20.
DAY0 = "2026-03-01T00:00:00+00:00"
DAY1 = "2026-03-02T00:00:00+00:00"
DAY3 = "2026-03-04T00:00:00+00:00"
DAY5 = "2026-03-06T00:00:00+00:00"
DAY20 = "2026-03-21T00:00:00+00:00"

KEY = "acct:nordvest.payment_status"


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    monkeypatch.setenv("ORLOG_VAULT_KEY", generate_key())


@pytest.fixture
def runtime(tmp_path):
    config = OrlogConfig(
        workspace=WorkspaceConfig(name="nordvest"),
        retrieval=RetrievalConfig(embedder="hashing", min_confidence=0.15, ambiguity_margin=0.85),
    )
    rt = Runtime(Workspace(tmp_path / "nordvest"), config)
    yield rt
    rt.close()


def _remember(runtime, value, *, valid_from, recorded):
    return remember_tool(
        runtime,
        f"Nordvest A/S payment_status is {value}.",
        entity="acct:nordvest", attribute="payment_status", value=value,
        evidence_span=f"payment_status is {value}",
        occurred_at=valid_from, recorded_at=recorded,
    )


def _seed(runtime):
    """day 0: current. day 3: the bad import says delinquent from day 1.
    day 20: the correction -- it was current from day 1 all along."""
    _remember(runtime, "current", valid_from=DAY0, recorded=DAY0)
    _remember(runtime, "delinquent", valid_from=DAY1, recorded=DAY3)
    _remember(runtime, "current", valid_from=DAY1, recorded=DAY20)


# ---------------------------------------------------------------- writes --

def test_remember_honours_a_caller_supplied_recorded_at(runtime):
    result = _remember(runtime, "current", valid_from=DAY0, recorded=DAY3)

    event = runtime.log.get(result["event_id"])
    assert event.recorded_at == datetime(2026, 3, 4, tzinfo=timezone.utc)
    assert event.occurred_at == datetime(2026, 3, 1, tzinfo=timezone.utc)


def test_remember_still_stamps_recorded_at_itself_when_not_given(runtime):
    before = datetime.now(timezone.utc)
    result = remember_tool(
        runtime, "Nordvest A/S payment_status is current.",
        entity="acct:nordvest", attribute="payment_status", value="current",
        evidence_span="payment_status is current", occurred_at=DAY0,
    )

    event = runtime.log.get(result["event_id"])
    assert before <= event.recorded_at <= datetime.now(timezone.utc)


def test_recorded_at_may_not_precede_occurred_at(runtime):
    # Event's own bi-temporal validator: you cannot record something before
    # it happened. Backfill must not become a way around that.
    with pytest.raises(SchemaError):
        _remember(runtime, "current", valid_from=DAY20, recorded=DAY0)


# ----------------------------------------------------------------- reads --

def test_the_regression_a_later_correction_rewrites_the_past_without_the_axis(runtime):
    """Without known_as_of this is what you get -- and why the axis exists."""
    _seed(runtime)

    # Asking about day 5, with everything on record: the day-20 correction
    # wins, so there is no trace of what was believed while acting.
    answer = recall_tool(runtime, KEY, as_of=DAY5)
    assert answer["claim"].endswith("= current")


def test_known_as_of_reconstructs_the_belief_the_agent_acted_on(runtime):
    _seed(runtime)

    # Same valid-time question, but only what had been recorded by day 5.
    believed = recall_tool(runtime, KEY, as_of=DAY5, known_as_of=DAY5)
    assert believed["claim"].endswith("= delinquent")
    assert believed["abstained"] is False
    assert believed["citations"], "a belief without provenance is a bug"

    # ...and today's record, same valid-time question, disagrees. That gap
    # is a memory failure rather than a reasoning failure.
    known_today = recall_tool(runtime, KEY, as_of=DAY5)
    assert known_today["claim"].endswith("= current")
    assert believed["claim"] != known_today["claim"]


def test_a_horizon_before_the_bad_import_sees_only_the_original(runtime):
    _seed(runtime)

    answer = recall_tool(runtime, KEY, as_of=DAY1, known_as_of=DAY1)
    assert answer["claim"].endswith("= current")


def test_a_fact_recorded_after_the_horizon_is_invisible_not_merely_outranked(runtime):
    # Only the day-20 correction exists. Ask below its horizon and there is
    # nothing to answer from at all -- it must abstain, not fall back.
    _remember(runtime, "current", valid_from=DAY1, recorded=DAY20)

    answer = recall_tool(runtime, KEY, as_of=DAY5, known_as_of=DAY5)
    assert answer["abstained"] is True
    assert answer["reasons"] == ["NO_CANDIDATES"]
    assert answer["claim"] is None


def test_omitting_known_as_of_is_unchanged_single_axis_behaviour(runtime):
    _seed(runtime)

    implicit = recall_tool(runtime, KEY, as_of=DAY5)
    explicit_none = recall_tool(runtime, KEY, as_of=DAY5, known_as_of=None)

    # Not a whole-dict comparison: `route` and `excluded` legitimately differ
    # because the second call hits the cache the first one populated -- which
    # is itself the point. An identical truth_version is what proves both
    # resolved to the SAME horizon and therefore the same cache key.
    assert implicit["truth_version"] == explicit_none["truth_version"]
    assert "|known@" not in implicit["truth_version"]
    for field in ("claim", "verified", "abstained", "reasons", "citations"):
        assert implicit[field] == explicit_none[field]


def test_two_horizons_do_not_collide_in_the_answer_cache(runtime):
    """The cache key is built from truth_version. Two horizons can project
    different views yet agree on built_from, so the horizon has to be part
    of that string or the second read is served the first's answer.
    """
    _seed(runtime)

    first = recall_tool(runtime, KEY, as_of=DAY5, known_as_of=DAY5)
    second = recall_tool(runtime, KEY, as_of=DAY5, known_as_of=DAY20)

    assert first["claim"].endswith("= delinquent")
    assert second["claim"].endswith("= current")
    assert first["truth_version"] != second["truth_version"]

    # And re-reading the first horizon still gives the first answer.
    assert recall_tool(runtime, KEY, as_of=DAY5, known_as_of=DAY5)["claim"] == first["claim"]


# --------------------------------------------------------------- history --

def test_recall_history_reports_recorded_at_per_revision(runtime):
    _seed(runtime)

    chain = recall_history_tool(runtime, KEY)["chain"]
    assert [c["recorded_at"] for c in chain] == [
        "2026-03-01T00:00:00+00:00",
        "2026-03-04T00:00:00+00:00",
        "2026-03-21T00:00:00+00:00",
    ]


def test_recall_history_truncates_to_the_horizon(runtime):
    _seed(runtime)

    chain = recall_history_tool(runtime, KEY, known_as_of=DAY5)["chain"]
    assert [c["value"] for c in chain] == ["current", "delinquent"]
    # The day-20 correction has not been learned yet at this horizon.
    assert all(c["recorded_at"] < "2026-03-21" for c in chain)
