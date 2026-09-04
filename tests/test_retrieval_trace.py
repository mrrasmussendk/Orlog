"""Contract: a read records itself (type="retrieval").

urd's events have always described writes. Two agents querying the same log
can end up with different contexts -- different horizons, different
valid-time questions, different abstentions -- so the retrieval is data too,
and a context cannot be reconstructed after the fact without it.
"""

from datetime import datetime, timezone

import pytest

from orlog.config import OrlogConfig, RetrievalConfig, WorkspaceConfig
from orlog.runtime import Runtime
from orlog.server_tools import (
    get_session_tool,
    list_sessions_tool,
    recall_tool,
    remember_tool,
)
from orlog.vault import generate_key
from orlog.workspace import Workspace

DAY1 = "2026-03-02T00:00:00+00:00"
DAY3 = "2026-03-04T00:00:00+00:00"
DAY5 = "2026-03-06T00:00:00+00:00"
DAY20 = "2026-03-21T00:00:00+00:00"

E = "acct:nordvest"
KEY = f"{E}.payment_status"
SESSION = "01JSESSIONAAAAAAAAAAAAAAAA"


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    monkeypatch.setenv("ORLOG_VAULT_KEY", generate_key())


@pytest.fixture
def runtime(tmp_path):
    config = OrlogConfig(
        workspace=WorkspaceConfig(name="trace"),
        retrieval=RetrievalConfig(embedder="hashing", min_confidence=0.15, ambiguity_margin=0.85),
    )
    rt = Runtime(Workspace(tmp_path / "trace"), config)
    _seed(rt)
    yield rt
    rt.close()


def _seed(runtime):
    remember_tool(
        runtime, "Nordvest A/S payment_status is delinquent.",
        entity=E, attribute="payment_status", value="delinquent",
        evidence_span="payment_status is delinquent", occurred_at=DAY1, recorded_at=DAY3,
    )
    remember_tool(
        runtime, "Nordvest A/S payment_status is current.",
        entity=E, attribute="payment_status", value="current",
        evidence_span="payment_status is current", occurred_at=DAY1, recorded_at=DAY20,
    )


def _retrievals(runtime, session_id=SESSION):
    return get_session_tool(runtime, session_id)["retrievals"]


# ------------------------------------------------------------- opt-in --

def test_a_recall_without_a_session_id_records_nothing(runtime):
    recall_tool(runtime, KEY, as_of=DAY5)

    assert list_sessions_tool(runtime)["sessions"] == []


def test_a_recall_with_a_session_id_records_what_it_was_asked_and_told(runtime):
    recall_tool(runtime, KEY, as_of=DAY5, known_as_of=DAY5, session_id=SESSION)

    record, = _retrievals(runtime)
    assert record["query"]["entity"] == E
    assert record["query"]["attribute"] == "payment_status"
    assert record["query"]["valid_at"].startswith("2026-03-06")
    assert record["query"]["known_as_of"].startswith("2026-03-06")
    assert record["result"]["value"] == "delinquent"
    assert record["result"]["verified"] is True
    assert record["result"]["abstained"] is False
    assert record["latency_ms"] >= 0


def test_the_recorded_result_distinguishes_two_horizons(runtime):
    # The whole point: the same valid-time question at two horizons must
    # leave two DIFFERENT records. outcome.verification alone cannot do this
    # -- it never stored the value.
    recall_tool(runtime, KEY, as_of=DAY5, known_as_of=DAY5, session_id=SESSION)
    recall_tool(runtime, KEY, as_of=DAY5, known_as_of=DAY20, session_id=SESSION)

    believed, known = _retrievals(runtime)
    assert believed["result"]["value"] == "delinquent"
    assert known["result"]["value"] == "current"


def test_citations_are_recorded_whole_not_as_bare_ids(runtime):
    # A context rebuilt from the trace needs the excerpt that went into the
    # prompt; a bare id would send the reader back to the log and
    # reintroduce the "which revision, at which horizon" ambiguity.
    recall_tool(runtime, KEY, as_of=DAY5, known_as_of=DAY5, session_id=SESSION)

    citation, = _retrievals(runtime)[0]["result"]["citations"]
    assert citation["event_id"]
    assert citation["excerpt"] == "payment_status is delinquent"
    assert citation["recorded_at"].startswith("2026-03-04")


# ------------------------------------------------- the resolved horizon --

def test_a_defaulted_horizon_is_still_written_down_resolved(runtime):
    """The non-negotiable. A horizon the system chose and did not record is
    the axis-collapse bug one level up."""
    recall_tool(runtime, KEY, as_of=DAY5, session_id=SESSION)

    query = _retrievals(runtime)[0]["query"]
    assert query["known_as_of"] is not None
    assert query["horizon_defaulted"] is True
    # A real timestamp, not the literal the caller passed (or didn't).
    datetime.fromisoformat(query["known_as_of"])


def test_no_trace_entry_ever_persists_a_null_or_unresolved_horizon(runtime):
    for as_of, known in [(DAY5, DAY5), (DAY5, None), ("now", None), ("now", DAY20)]:
        kwargs = {"as_of": as_of, "session_id": SESSION}
        if known is not None:
            kwargs["known_as_of"] = known
        recall_tool(runtime, KEY, **kwargs)

    records = _retrievals(runtime)
    assert len(records) == 4
    for record in records:
        for axis in ("valid_at", "known_as_of"):
            value = record["query"][axis]
            assert value is not None, f"{axis} was null"
            assert value != "now", f"{axis} was persisted unresolved"
            parsed = datetime.fromisoformat(value)
            assert parsed.tzinfo is not None, f"{axis} was persisted without an offset"


def test_an_explicit_horizon_is_not_marked_defaulted(runtime):
    recall_tool(runtime, KEY, as_of=DAY5, known_as_of=DAY5, session_id=SESSION)

    assert _retrievals(runtime)[0]["query"]["horizon_defaulted"] is False


# ----------------------------------------------------- steps & sessions --

def test_steps_are_assigned_by_the_log_and_are_monotonic(runtime):
    for _ in range(3):
        recall_tool(runtime, KEY, as_of=DAY5, session_id=SESSION)

    assert [r["step"] for r in _retrievals(runtime)] == [0, 1, 2]


def test_two_sessions_number_their_steps_independently(runtime):
    other = "01JSESSIONBBBBBBBBBBBBBBBB"
    recall_tool(runtime, KEY, as_of=DAY5, session_id=SESSION)
    recall_tool(runtime, KEY, as_of=DAY5, session_id=other)
    recall_tool(runtime, KEY, as_of=DAY5, session_id=SESSION)

    assert [r["step"] for r in _retrievals(runtime)] == [0, 1]
    assert [r["step"] for r in _retrievals(runtime, other)] == [0]


def test_list_sessions_reports_every_traced_run(runtime):
    other = "01JSESSIONBBBBBBBBBBBBBBBB"
    recall_tool(runtime, KEY, as_of=DAY5, session_id=SESSION)
    recall_tool(runtime, KEY, as_of=DAY5, session_id=other)
    recall_tool(runtime, KEY, as_of=DAY5, session_id=SESSION)

    by_id = {s["session_id"]: s for s in list_sessions_tool(runtime)["sessions"]}
    assert by_id[SESSION]["steps"] == 2
    assert by_id[other]["steps"] == 1


def test_an_unknown_session_is_empty_not_an_error(runtime):
    assert get_session_tool(runtime, "01JNOSUCHSESSION")["retrievals"] == []


# ---------------------------------------------------------- abstention --

def test_an_abstention_is_traced_with_its_reasons(runtime):
    recall_tool(runtime, f"{E}.no_such_attribute", as_of=DAY5, session_id=SESSION)

    record, = _retrievals(runtime)
    assert record["result"]["abstained"] is True
    assert record["result"]["reasons"] == ["NO_CANDIDATES"]
    assert record["result"]["value"] is None
    assert record["result"]["citations"] == []
    # Still a fully resolved horizon: an abstention is a read like any other.
    assert record["query"]["known_as_of"] is not None


# ------------------------------------------------------------ integrity --

def test_retrieval_events_ride_the_same_hash_chain_as_facts(runtime):
    recall_tool(runtime, KEY, as_of=DAY5, session_id=SESSION)
    recall_tool(runtime, KEY, as_of=DAY5, known_as_of=DAY20, session_id=SESSION)

    assert runtime.log.verify_chain(full=True)
    kinds = {e.type for e in runtime.log.read_all()}
    assert {"fact", "retrieval"} <= kinds


def test_retrieval_events_do_not_disturb_the_fact_projections(runtime):
    before = recall_tool(runtime, KEY, as_of=DAY5, known_as_of=DAY20)
    recall_tool(runtime, KEY, as_of=DAY5, session_id=SESSION)
    after = recall_tool(runtime, KEY, as_of=DAY5, known_as_of=DAY20)

    assert before["claim"] == after["claim"]
    assert before["truth_version"] == after["truth_version"]
