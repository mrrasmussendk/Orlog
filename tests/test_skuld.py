"""Contract: skuld's outcome recording and adaptation (ORLOG-SPEC.md §4.6/§B6):
importance defaults to 1.0, a pass rewards cited facts, decay shrinks and
floors every weight, and every outcome (including corrections) is appended
to the log.
"""

from datetime import datetime, timezone

from orlog.skuld import DECAY_FLOOR, OutcomeLedger
from orlog.storage import EventIndex

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_importance_defaults_to_one(make_log):
    ledger = OutcomeLedger(make_log())
    assert ledger.importance("ev-1") == 1.0


def test_reward_pass_bumps_each_cited_fact(make_log):
    ledger = OutcomeLedger(make_log())
    ledger.reward_pass(["ev-1", "ev-2"], bonus=0.5, now=NOW)

    assert ledger.importance("ev-1") == 1.5
    assert ledger.importance("ev-2") == 1.5
    ledger.reward_pass(["ev-1"], bonus=0.5, now=NOW)
    assert ledger.importance("ev-1") == 2.0
    assert ledger.importance("ev-2") == 1.5  # untouched


def test_decay_shrinks_every_known_weight(make_log):
    ledger = OutcomeLedger(make_log())
    ledger.reward_pass(["ev-1"], bonus=1.0, now=NOW)  # weight now 2.0

    ledger.apply_decay(factor=0.5, floor=0.1, now=NOW)

    assert ledger.importance("ev-1") == 1.0


def test_decay_floors_at_the_configured_minimum(make_log):
    ledger = OutcomeLedger(make_log())
    ledger.reward_pass(["ev-1"], bonus=-0.95, now=NOW)  # weight now ~0.05

    ledger.apply_decay(factor=0.5, floor=DECAY_FLOOR, now=NOW)

    assert ledger.importance("ev-1") == DECAY_FLOOR


def test_record_correction_appends_an_outcome_event_and_rewards_the_corrected_fact(make_log):
    log = make_log()
    ledger = OutcomeLedger(log)

    ledger.record_correction(
        query_id="q1", corrected_by="user", note="the real plan is pro",
        corrected_to_event_id="ev-2", occurred_at=NOW, bonus=1.0,
    )

    outcome_events = [e for e in log.read_all() if e.type == "outcome.correction"]
    assert len(outcome_events) == 1
    assert outcome_events[0].payload["corrected_to_event_id"] == "ev-2"
    assert ledger.importance("ev-2") == 2.0


def test_importance_persists_to_the_sqlite_index_when_one_is_supplied(tmp_path, make_log):
    log = make_log()
    index = EventIndex(tmp_path / "index.sqlite")
    ledger = OutcomeLedger(log, index=index)

    ledger.reward_pass(["ev-1"], bonus=0.5, now=NOW)

    row = index._conn.execute("SELECT weight FROM importance WHERE event_id = ?", ("ev-1",)).fetchone()
    assert row[0] == 1.5
    assert ledger.importance("ev-1") == 1.5
