"""skuld: L5 outcome ledger & adaptation -- what is owed (ORLOG-SPEC.md §4.6/§7/§B6).

Design decisions:

1. Two explicit recording methods (record_verification, record_abstention)
   instead of spec §4.6's single polymorphic `record(result)`. Spec §4
   states "Signatures below are given in Python; the normative definition
   is the JSON schema of inputs/outputs plus the MUST clauses" -- so the
   exact method shape is non-normative. Two explicit methods avoid a
   circular import (Abstention/Answer live in pipeline.py, which needs to
   import OutcomeLedger; a polymorphic record() would need the reverse
   import) and are simpler to read.

2. Importance is persisted to the SQLite `importance` table (storage.py's
   DDL) when an `index` (storage.EventIndex) is supplied; otherwise it
   falls back to an in-memory dict, so OutcomeLedger stays usable without a
   full workspace (as every conformance test does). Either way, weight
   starts at 1.0 for anything never yet rewarded (spec §4.6 Protocol).

3. FORBIDDEN inputs (spec §B6, §7.2, and the empirical basis in
   DESIGN-PRINCIPLES.md principle 6): retrieval co-occurrence, query
   frequency without outcomes, embedding drift. This class simply never
   accepts those as inputs to reward_pass/reward_correction/apply_decay --
   there is no code path here that could touch them.

4. apply_decay() is not scheduled by this class -- spec §B6 calls it
   "nightly decay", which is a job someone (orlog serve's own loop, or an
   external cron hitting the workspace) triggers on a schedule; skuld only
   provides the operation.
"""

from __future__ import annotations

from datetime import datetime

from orlog.heimdall import VerificationResult
from orlog.models.event import Event, EventDraft
from orlog.urd import EventLog

DEFAULT_PASS_BONUS = 0.5
DEFAULT_CORRECTION_BONUS = 1.0
DEFAULT_DAILY_DECAY = 0.99
DECAY_FLOOR = 0.1


class OutcomeLedger:
    def __init__(self, log: EventLog, *, index=None) -> None:
        self._log = log
        self._index = index  # storage.EventIndex | None
        self._importance: dict[str, float] = {}  # used only when index is None

    # -- recording outcomes (T5: every terminal state emits an event) --

    def record_verification(self, result: VerificationResult) -> Event:
        """Append an outcome.verification event -- called on every completed
        verification, pass or fail.
        """
        payload = {
            "assertion_query_id": result.assertion_query_id,
            "status": result.status,
            "truth_version": result.truth_version,
            "failures": [f.model_dump(mode="json") for f in result.failures],
        }
        return self._log.append(
            EventDraft(occurred_at=result.checked_at, actor="system", type="outcome.verification", payload=payload)
        )

    def record_abstention(self, *, query_id: str, reasons: list[str], occurred_at: datetime) -> Event:
        """Append an outcome.abstention event (spec §7.1: {query_id, reasons})."""
        return self._log.append(
            EventDraft(
                occurred_at=occurred_at,
                actor="system",
                type="outcome.abstention",
                payload={"query_id": query_id, "reasons": reasons},
            )
        )

    def record_correction(self, *, query_id: str, corrected_by: str, note: str, corrected_to_event_id: str, occurred_at: datetime, bonus: float = DEFAULT_CORRECTION_BONUS) -> Event:
        """Append an outcome.correction event (spec §7.1: {query_id,
        corrected_by, note}) and reward the corrected-to fact's importance.
        """
        self._bump_importance(corrected_to_event_id, bonus, now=occurred_at)
        return self._log.append(
            EventDraft(
                occurred_at=occurred_at,
                actor=corrected_by,
                type="outcome.correction",
                payload={"query_id": query_id, "corrected_by": corrected_by, "note": note, "corrected_to_event_id": corrected_to_event_id},
            )
        )

    # -- adaptation (spec §4.6/§B6): outcome-driven only --

    def reward_pass(self, citations: list[str], *, bonus: float = DEFAULT_PASS_BONUS, now: datetime) -> None:
        """Each citation on a VERIFIED assertion earns `bonus` importance."""
        for event_id in citations:
            self._bump_importance(event_id, bonus, now=now)

    def apply_decay(self, *, factor: float = DEFAULT_DAILY_DECAY, floor: float = DECAY_FLOOR, now: datetime) -> None:
        """Multiply every known weight by `factor`, floored at `floor`."""
        if self._index is not None:
            rows = self._index._conn.execute("SELECT event_id, weight FROM importance").fetchall()
            for event_id, weight in rows:
                self._write_importance(event_id, max(weight * factor, floor), now=now)
        else:
            for event_id, weight in list(self._importance.items()):
                self._importance[event_id] = max(weight * factor, floor)

    def importance(self, event_id: str) -> float:
        """spec §4.6 Protocol. Default weight is 1.0 until rewarded."""
        if self._index is not None:
            row = self._index._conn.execute("SELECT weight FROM importance WHERE event_id = ?", (event_id,)).fetchone()
            return row[0] if row is not None else 1.0
        return self._importance.get(event_id, 1.0)

    def _bump_importance(self, event_id: str, delta: float, *, now: datetime) -> None:
        self._write_importance(event_id, self.importance(event_id) + delta, now=now)

    def _write_importance(self, event_id: str, weight: float, *, now: datetime) -> None:
        if self._index is not None:
            self._index._conn.execute(
                "INSERT INTO importance (event_id, weight, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(event_id) DO UPDATE SET weight = excluded.weight, updated_at = excluded.updated_at",
                (event_id, weight, now.isoformat()),
            )
            self._index._conn.commit()
        else:
            self._importance[event_id] = weight
