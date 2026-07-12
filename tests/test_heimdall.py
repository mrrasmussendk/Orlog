"""Contract: heimdall's four checks (V1-V4), each in isolation, per
ORLOG-SPEC.md §4.5.
"""

from datetime import datetime, timezone

from orlog.heimdall import GroundTruthFact, Heimdall
from orlog.huginn import Assertion

T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 6, 1, tzinfo=timezone.utc)
CHECKED_AT = datetime(2026, 7, 1, tzinfo=timezone.utc)

TRUTH = {
    "ev-1": GroundTruthFact(fact_id="ev-1", entity="user:1", attribute="plan", value="free", valid_from=T1, valid_to=T2),
    "ev-2": GroundTruthFact(fact_id="ev-2", entity="user:1", attribute="plan", value="pro", valid_from=T2, valid_to=datetime(9999, 12, 31, tzinfo=timezone.utc)),
}


def _assertion(**overrides):
    fields = dict(
        query_id="q1",
        claim="user:1.plan = pro",
        citations=["ev-2"],
        derived_by="test",
        derived_at=CHECKED_AT,
        route="fresh",
    )
    fields.update(overrides)
    return Assertion(**fields)


def test_v1_uncited_fails_first_and_alone():
    verifier = Heimdall(TRUTH, truth_version="t1")
    result = verifier.verify(_assertion(citations=[]), T2, checked_at=CHECKED_AT)

    assert result.status == "fail"
    assert len(result.failures) == 1
    assert result.failures[0].code == "UNCITED"
    assert result.failures[0].citation == "<uncited>"


def test_v2_exists_fails_on_an_unknown_citation():
    verifier = Heimdall(TRUTH, truth_version="t1")
    result = verifier.verify(_assertion(citations=["nonexistent"]), T2, checked_at=CHECKED_AT)

    assert result.status == "fail"
    assert result.failures[0].code == "NOT_FOUND"


def test_v3_valid_fails_when_expired():
    verifier = Heimdall(TRUTH, truth_version="t1")
    # ev-1 ("free") was valid [T1, T2) -- querying as_of T2 means it has expired.
    result = verifier.verify(_assertion(claim="user:1.plan = free", citations=["ev-1"]), T2, checked_at=CHECKED_AT)

    assert result.status == "fail"
    assert result.failures[0].code == "EXPIRED"


def test_v3_valid_fails_when_not_yet_valid():
    verifier = Heimdall(TRUTH, truth_version="t1")
    # ev-2 ("pro") only becomes valid at T2 -- querying as_of T1 is too early.
    result = verifier.verify(_assertion(citations=["ev-2"]), T1, checked_at=CHECKED_AT)

    assert result.status == "fail"
    assert result.failures[0].code == "NOT_YET_VALID"


def test_v4_supports_fails_when_claim_does_not_contain_the_cited_value():
    verifier = Heimdall(TRUTH, truth_version="t1")
    result = verifier.verify(_assertion(claim="user:1.plan = enterprise", citations=["ev-2"]), T2, checked_at=CHECKED_AT)

    assert result.status == "fail"
    assert result.failures[0].code == "UNSUPPORTED"


def test_v4_supports_fails_when_the_value_matches_but_the_key_belongs_to_a_different_fact():
    # A claim that happens to contain the cited fact's real value, but under
    # the WRONG key, must still fail -- this is the C3 mis-grouping scenario:
    # citation is real, current, and its value is textually present, but it
    # is not actually about what the claim says it's about.
    verifier = Heimdall(TRUTH, truth_version="t1")
    result = verifier.verify(_assertion(claim="user:9.plan = pro", citations=["ev-2"]), T2, checked_at=CHECKED_AT)

    assert result.status == "fail"
    assert result.failures[0].code == "UNSUPPORTED"


def test_all_four_checks_pass_together():
    verifier = Heimdall(TRUTH, truth_version="t1")
    result = verifier.verify(_assertion(), T2, checked_at=CHECKED_AT)

    assert result.status == "pass"
    assert result.failures == []
    assert result.truth_version == "t1"


def test_would_pass_validity_matches_v2_plus_v3():
    verifier = Heimdall(TRUTH, truth_version="t1")

    assert verifier.would_pass_validity("ev-2", T2) is True
    assert verifier.would_pass_validity("ev-1", T2) is False  # expired
    assert verifier.would_pass_validity("ev-2", T1) is False  # not yet valid
    assert verifier.would_pass_validity("nonexistent", T2) is False
