"""Shared pytest fixtures: small factories for building valid model
instances with sensible defaults, so individual tests only need to specify
the field(s) they actually care about.
"""

from datetime import datetime, timezone

import pytest

from orlog.models.assertion import Assertion, Citation
from orlog.models.event import EventDraft
from orlog.models.verification import VerificationChecks, VerificationResult
from orlog.urd import EventLog

# A fixed point in time, used everywhere instead of datetime.now(), so tests
# are deterministic and never flaky around a real clock.
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

# EventLog.append() assigns recorded_at from a clock (real UTC now, by
# default) and requires recorded_at >= occurred_at. Test fixtures use
# occurred_at values well into "the future" relative to whatever the real
# wall-clock date happens to be when tests run (they're modeling a whole
# timeline, not "today"), so every EventLog built in a test is given this
# fixed, far-future clock instead of the real one -- safely after every
# occurred_at used anywhere in this suite.
TEST_CLOCK_NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def make_event():
    """Builds an EventDraft (occurred_at/actor/type/payload/entities), NOT a
    full Event -- id/recorded_at/prev_hash are only assigned once you call
    EventLog.append() on the draft this returns.
    """

    def _make(**overrides):
        fields = dict(
            occurred_at=NOW,
            actor="test",
            type="fact",
            payload={"note": "hello"},
        )
        fields.update(overrides)
        return EventDraft(**fields)

    return _make


@pytest.fixture
def make_log(tmp_path):
    """Builds an EventLog in a fresh tmp_path file, using TEST_CLOCK_NOW so
    recorded_at is always deterministic and always after every test's
    occurred_at values.
    """

    def _make(filename: str = "log.jsonl") -> EventLog:
        return EventLog(tmp_path / filename, clock=lambda: TEST_CLOCK_NOW)

    return _make


@pytest.fixture
def make_citation():
    def _make(**overrides):
        fields = dict(citation_id="ev-1")
        fields.update(overrides)
        return Citation(**fields)

    return _make


@pytest.fixture
def make_assertion(make_citation):
    def _make(**overrides):
        fields = dict(
            assertion_id="a-1",
            claim="the sky is blue",
            citations=[make_citation()],
            derived_at=NOW,
        )
        fields.update(overrides)
        return Assertion(**fields)

    return _make


@pytest.fixture
def fake_verify():
    """A trivial in-memory fake of heimdall, for tests that only care about
    the gating *contract* (VERIFIED requires a pass, a fail needs a reason)
    and not about a real verifier's logic -- see orlog/heimdall.py for the
    real one, exercised by tests/conformance/.
    """

    def _verify(assertion, citation, *, passes: bool, verification_id: str, reason: str | None = None):
        checks = VerificationChecks(exists=passes, currently_valid=passes, supports_claim=passes)
        return VerificationResult(
            verification_id=verification_id,
            assertion_id=assertion.assertion_id,
            citation_id=citation.citation_id,
            claim=assertion.claim,
            as_of=NOW,
            checks=checks,
            passed=passes,
            reason=None if passes else (reason or "fake heimdall: forced failure"),
        )

    return _verify
