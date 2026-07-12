"""Contract: an assertion cannot reach VERIFIED without a heimdall pass, and
a failed verification triggers exactly one re-derivation before ABSTAINED.

This test proves the contract abstractly, against the `fake_verify` fixture
(tests/conftest.py) rather than the real heimdall (orlog/heimdall.py) --
it's a fast, dependency-free check that states.py's transition rules
correctly support "verify, retry once, else abstain" for *any* verifier.
tests/conformance/ exercises the same shape end to end against the real
heimdall + a real log.
"""

from orlog.states import EpistemicState, transition


def _rederive(assertion):
    """Stand-in for huginn: produce a new assertion attempt, linked via retry_of."""
    return assertion.model_copy(
        update={
            "assertion_id": assertion.assertion_id + "-retry",
            "retry_of": assertion.assertion_id,
        }
    )


def _gate(assertion, citation, verify_fn, rederive_fn):
    """Verify once; on failure, re-derive and verify exactly once more."""
    attempt = assertion
    results = []

    result = verify_fn(attempt, citation, verification_id=f"{attempt.assertion_id}-v1")
    results.append(result)
    if result.passed:
        return transition(EpistemicState.ASSERTED, EpistemicState.VERIFIED), results

    attempt = rederive_fn(attempt)
    result = verify_fn(attempt, citation, verification_id=f"{attempt.assertion_id}-v1")
    results.append(result)
    if result.passed:
        return transition(EpistemicState.ASSERTED, EpistemicState.VERIFIED), results

    return transition(EpistemicState.ASSERTED, EpistemicState.ABSTAINED), results


def test_assertion_reaches_verified_only_after_a_pass(make_assertion, fake_verify):
    assertion = make_assertion()
    citation = assertion.citations[0]
    calls = []

    def always_pass(a, c, **kwargs):
        calls.append(a.assertion_id)
        return fake_verify(a, c, passes=True, **kwargs)

    final_state, results = _gate(assertion, citation, always_pass, _rederive)

    assert final_state == EpistemicState.VERIFIED
    assert len(calls) == 1  # passed first time, no re-derivation needed
    assert results[-1].passed is True


def test_failed_verification_triggers_exactly_one_rederivation_then_abstains(make_assertion, fake_verify):
    assertion = make_assertion()
    citation = assertion.citations[0]
    calls = []

    def always_fail(a, c, **kwargs):
        calls.append(a.assertion_id)
        return fake_verify(a, c, passes=False, reason="citation superseded", **kwargs)

    final_state, results = _gate(assertion, citation, always_fail, _rederive)

    assert final_state == EpistemicState.ABSTAINED
    assert len(calls) == 2  # original attempt + exactly one re-derivation, then stop
    assert calls[1] == assertion.assertion_id + "-retry"
    assert all(r.passed is False for r in results)
    assert all(r.reason for r in results)  # heimdall must give a reason on failure


def test_a_passing_rederivation_still_reaches_verified(make_assertion, fake_verify):
    assertion = make_assertion()
    citation = assertion.citations[0]
    calls = []

    def fail_then_pass(a, c, **kwargs):
        calls.append(a.assertion_id)
        return fake_verify(a, c, passes=len(calls) > 1, **kwargs)

    final_state, results = _gate(assertion, citation, fail_then_pass, _rederive)

    assert final_state == EpistemicState.VERIFIED
    assert len(calls) == 2
    assert results[0].passed is False
    assert results[1].passed is True
