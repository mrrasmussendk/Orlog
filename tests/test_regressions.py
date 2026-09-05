"""Regressions for defects found in a systematic audit of the codebase.

Each test here pins a specific bug that shipped, described in terms of the
wrong behaviour it produced rather than the code that produced it. Grouped
by subsystem; the verification-gate ones live in test_heimdall.py next to
the V4 tests they belong with.
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from orlog.config import OrlogConfig, RetrievalConfig, WorkspaceConfig
from orlog.errors import LockedError, SchemaError, StorageError
from orlog.models.event import EventDraft
from orlog.runtime import Runtime
from orlog.server_tools import (
    check_action_tool,
    list_attributes_tool,
    recall_tool,
    remember_tool,
    stats_tool,
)
from orlog.storage import SegmentedLog
from orlog.urd import EventLog, read_intact_events
from orlog.vault import Vault, generate_key
from orlog.workspace import Workspace

T1 = "2026-01-01T00:00:00+00:00"
T2 = "2026-06-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    monkeypatch.setenv("ORLOG_VAULT_KEY", generate_key())


@pytest.fixture
def runtime(tmp_path):
    config = OrlogConfig(
        workspace=WorkspaceConfig(name="reg"),
        retrieval=RetrievalConfig(embedder="hashing", min_confidence=0.15, ambiguity_margin=0.85),
    )
    rt = Runtime(Workspace(tmp_path / "reg"), config)
    yield rt
    rt.close()


def _draft(text="x"):
    return EventDraft(
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        actor="u", type="fact", payload={"text": text},
    )


# --- storage: the log must notice when its own tail goes missing ----------


def test_verify_chain_detects_a_truncated_tail(tmp_path):
    # Forward links alone cannot see this: lopping events off the END
    # leaves a shorter chain that is still internally consistent, so
    # verify_chain reported "verified" on a log that had silently lost its
    # most recent memories.
    events_dir = tmp_path / "events"
    log = SegmentedLog(events_dir, rotate_bytes=400)
    for i in range(12):
        log.append(_draft(f"event {i}"))

    assert SegmentedLog(events_dir).verify_chain(full=True) is True

    segments = sorted(events_dir.glob("log-*.jsonl"))
    segments[-1].unlink()
    kept = segments[-2].read_text(encoding="utf-8").splitlines()[:-1]
    segments[-2].write_text("\n".join(kept) + "\n", encoding="utf-8")

    assert SegmentedLog(events_dir).verify_chain(full=True) is False


def test_a_torn_final_record_is_a_typed_error_not_a_json_traceback(tmp_path):
    # What a crash mid-append leaves behind. EventLog.__init__ reads the
    # log to compute the chain head, so an unhandled JSONDecodeError here
    # made the whole workspace impossible to even open.
    path = tmp_path / "log.jsonl"
    log = EventLog(path)
    for i in range(3):
        log.append(_draft(f"e{i}"))
    path.write_bytes(path.read_bytes()[:-20])

    with pytest.raises(StorageError) as exc_info:
        EventLog(path).read_all()
    assert "final record" in str(exc_info.value)

    # The intact prefix is still recoverable -- refusing to open a damaged
    # log must not mean the surviving events are lost with it.
    assert len(read_intact_events(path)) == 2


def test_rotation_does_not_reuse_an_existing_segment_number(tmp_path):
    # Numbering from len(segments) rather than the highest existing number
    # collided whenever there was a gap, appending a duplicate path so the
    # same segment's events were read twice.
    events_dir = tmp_path / "events"
    log = SegmentedLog(events_dir, rotate_bytes=400)
    for i in range(12):
        log.append(_draft(f"event {i}"))
    segments = sorted(events_dir.glob("log-*.jsonl"))
    assert len(segments) > 2

    first = segments[0]
    archived = first.read_bytes()
    first.unlink()  # archived away, leaving a gap in the numbering

    reopened = SegmentedLog(events_dir, rotate_bytes=1)
    reopened.append(_draft("after the gap"))

    paths = list(reopened._segment_paths)
    assert len(paths) == len(set(paths)), "a segment path was added twice"
    first.write_bytes(archived)


# --- workspace: the lock has to actually exclude -------------------------


def test_a_second_holder_cannot_take_a_live_lock(tmp_path):
    ws_a = Workspace(tmp_path / "ws")
    ws_b = Workspace(tmp_path / "ws")
    ws_a.scaffold()

    with ws_a:
        with pytest.raises(LockedError):
            ws_b.acquire_lock()


def test_a_stale_lock_from_a_dead_process_is_reclaimed(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.scaffold()
    ws.data_dir.mkdir(parents=True, exist_ok=True)
    ws.lock_path.write_text("999999", encoding="utf-8")  # a pid that isn't running

    ws.acquire_lock()  # must not raise
    ws.release_lock()


# --- vault: a forgotten token is retired, not recycled -------------------


def test_a_forgotten_token_is_never_reissued(tmp_path):
    # Reissuing a crypto-shredded token re-points every already-logged
    # event that references it at a different subject.
    vault = Vault(tmp_path / "vault.sqlite")
    issued = [vault.tokenize(f"user{i}@example.com", kind="EMAIL") for i in range(1, 6)]
    vault.forget(issued[-1])

    assert vault.tokenize("someone-new@example.com", kind="EMAIL") not in issued
    vault.close()

    reopened = Vault(tmp_path / "vault.sqlite")  # and the retirement survives a reopen
    assert reopened.tokenize("another@example.com", kind="EMAIL") not in issued
    reopened.close()


# --- recall: routing and honesty ----------------------------------------


def test_a_period_in_prose_does_not_hijack_the_exact_key_path(runtime):
    # rpartition(".") fired on any query containing a period, so ordinary
    # prose was routed to exact-key lookup, missed, and returned
    # NO_CANDIDATES for a fact that was on record.
    remember_tool(runtime, "Anna lives in Porto", occurred_at=T1,
                  entity="anna", attribute="city", value="Porto")

    plain = recall_tool(runtime, "Where does Anna live", as_of=T2)
    dotted = recall_tool(runtime, "Where does Anna live?.", as_of=T2)

    assert plain["verified"] is True
    assert dotted["verified"] == plain["verified"]
    assert dotted["claim"] == plain["claim"]


def test_a_real_dotted_key_still_takes_the_exact_path(runtime):
    remember_tool(runtime, "dark theme", occurred_at=T1,
                  entity="user:1.settings", attribute="theme", value="dark")

    result = recall_tool(runtime, "user:1.settings.theme", as_of=T2)

    assert result["verified"] is True
    assert result["claim"] == "user:1.settings.theme = dark"


def test_repeated_identical_recalls_hit_the_route_cache(runtime):
    # Keying on as_of.isoformat() made the default as_of="now" key unique
    # per microsecond, so the cache could never hit and every repeat paid a
    # full derive.
    remember_tool(runtime, "plan is pro", occurred_at=T1,
                  entity="user:1", attribute="plan", value="pro")

    routes = [recall_tool(runtime, "user:1.plan")["route"] for _ in range(4)]

    assert routes[0] == "fresh"
    assert routes[1:] == ["cache", "cache", "cache"]
    assert stats_tool(runtime)["cache_hits"] == 3


def test_a_cache_hit_reports_the_same_exclusions_as_the_fresh_answer(runtime):
    # The cached route served hardcoded zeros, so the identical question
    # answered twice reported different exclusion counts.
    remember_tool(runtime, "plan is free", occurred_at=T1,
                  entity="user:1", attribute="plan", value="free")
    remember_tool(runtime, "plan is pro", occurred_at=T2,
                  entity="user:1", attribute="plan", value="pro")

    fresh = recall_tool(runtime, "user:1.plan")
    cached = recall_tool(runtime, "user:1.plan")

    assert cached["route"] == "cache"
    assert cached["excluded"] == fresh["excluded"]
    assert fresh["excluded"]["by_validity"] == 1


def test_a_malformed_as_of_is_a_schema_error_not_a_valueerror(runtime):
    with pytest.raises(SchemaError):
        recall_tool(runtime, "user:1.plan", as_of="yesterday")


# --- list_attributes: "current" must mean current ------------------------


def test_list_attributes_reports_the_currently_valid_value(runtime):
    # Taking the last entry in the chain reported a backfilled future fact
    # as the current value, contradicting what recall() served for the very
    # same key.
    future = (datetime.now(timezone.utc) + timedelta(days=365 * 5)).isoformat()
    remember_tool(runtime, "city is Porto", occurred_at=T1,
                  entity="bob", attribute="city", value="Porto")
    remember_tool(runtime, "city is Berlin", occurred_at=future, recorded_at=future,
                  entity="bob", attribute="city", value="Berlin")

    listed = list_attributes_tool(runtime, "bob")
    recalled = recall_tool(runtime, "bob.city")

    city = next(a for a in listed["attributes"] if a["attribute"] == "city")
    assert city["value"] == "Porto"
    assert recalled["claim"] == "bob.city = Porto"


# --- check_action: no warnings out of thin air ---------------------------


def test_check_action_rejects_an_empty_description(runtime):
    # An unanchored substring match meant "" matched every outcome event,
    # manufacturing a governance warning from a no-op argument.
    with pytest.raises(SchemaError):
        check_action_tool(runtime, "")
    with pytest.raises(SchemaError):
        check_action_tool(runtime, "   ")


# --- write path: PII that can never be scrubbed is refused ---------------


def test_remember_refuses_pii_in_the_entity_key(runtime):
    # entity is never scrubbed (scrubbing it would break exact-match
    # recall), so an email there lands in the append-only log in cleartext,
    # permanently beyond `orlog forget`.
    with pytest.raises(SchemaError) as exc_info:
        remember_tool(runtime, "their plan is pro", occurred_at=T1,
                      entity="user:alice@example.com", attribute="plan", value="pro")
    assert "PII_IN_KEY" in str(exc_info.value)


def test_remember_still_accepts_an_ordinary_opaque_entity(runtime):
    event_id = remember_tool(runtime, "their plan is pro", occurred_at=T1,
                             entity="user:alice", attribute="plan", value="pro")["event_id"]
    assert event_id
