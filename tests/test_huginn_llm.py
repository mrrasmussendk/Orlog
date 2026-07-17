"""Contract: LLMDeriver honors the spec §B4 prompt contract's MUST clauses,
exercised entirely against a fake CompletionFn -- no network call, no API
key, ever, in this test file.
"""

from datetime import datetime, timezone

import pytest

from orlog.huginn import DeriverInsufficientEvidence, DeriverTimeout, UncitedAssertion
from orlog.huginn_llm import INSUFFICIENT_TOKEN, LLMDeriver
from orlog.retrieval import Candidate

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _candidate(event_id="ev-1", content="pro", excerpt=None):
    return Candidate(event_id=event_id, content=content, valid_from=NOW, valid_to=NOW, score=1.0, excerpt=excerpt)


class _QueuedCompletion:
    """A fake CompletionFn: returns each queued (text, usage) pair in turn."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = 0

    def __call__(self, system, user, *, max_tokens):
        self.calls += 1
        if not self._responses:
            raise AssertionError("ran out of queued responses")
        return self._responses.pop(0)


def test_happy_path_produces_an_assertion_with_tokens_and_latency():
    completion = _QueuedCompletion(('{"claim": "user:1.plan = pro", "citations": ["ev-1"]}', {"in": 10, "out": 5}))
    deriver = LLMDeriver(completion, model="test-model")

    assertion = deriver.derive("user:1.plan", NOW, [_candidate()], query_id="q1", derived_at=NOW, route="fresh")

    assert assertion.claim == "user:1.plan = pro"
    assert assertion.citations == ["ev-1"]
    assert assertion.tokens == {"in": 10, "out": 5}
    assert assertion.latency_ms is not None and assertion.latency_ms >= 0
    assert completion.calls == 1


def test_insufficient_token_raises_deriver_insufficient_evidence():
    completion = _QueuedCompletion((INSUFFICIENT_TOKEN, {"in": 5, "out": 1}))
    deriver = LLMDeriver(completion, model="test-model")

    with pytest.raises(DeriverInsufficientEvidence):
        deriver.derive("user:1.plan", NOW, [_candidate()], query_id="q1", derived_at=NOW, route="fresh")


def test_malformed_json_then_valid_json_succeeds_via_one_repair_retry():
    completion = _QueuedCompletion(
        ("not json at all", {"in": 5, "out": 5}),
        ('{"claim": "user:1.plan = pro", "citations": ["ev-1"]}', {"in": 8, "out": 4}),
    )
    deriver = LLMDeriver(completion, model="test-model")

    assertion = deriver.derive("user:1.plan", NOW, [_candidate()], query_id="q1", derived_at=NOW, route="fresh")

    assert assertion.claim == "user:1.plan = pro"
    assert assertion.tokens == {"in": 13, "out": 9}  # summed across both calls
    assert completion.calls == 2


def test_malformed_json_twice_raises_uncited_assertion():
    completion = _QueuedCompletion(
        ("still not json", {"in": 5, "out": 5}),
        ("still not json either", {"in": 5, "out": 5}),
    )
    deriver = LLMDeriver(completion, model="test-model")

    with pytest.raises(UncitedAssertion):
        deriver.derive("user:1.plan", NOW, [_candidate()], query_id="q1", derived_at=NOW, route="fresh")

    assert completion.calls == 2  # exactly one repair retry, then give up


def test_hallucinated_citation_ids_are_filtered_and_none_remaining_raises():
    completion = _QueuedCompletion(('{"claim": "user:1.plan = pro", "citations": ["not-a-real-id"]}', {"in": 5, "out": 5}))
    deriver = LLMDeriver(completion, model="test-model")

    with pytest.raises(UncitedAssertion):
        deriver.derive("user:1.plan", NOW, [_candidate()], query_id="q1", derived_at=NOW, route="fresh")


def test_prompt_prefers_excerpt_over_bare_content_when_present():
    """A bare stored value with no attribute label or grounding sentence
    was verified (against real Anthropic and OpenAI models) to reliably
    produce a spurious INSUFFICIENT, even on orlog's own README example --
    see retrieval.py's Candidate.excerpt docstring. _render_facts must
    prefer it over the bare `content` whenever it's set.
    """
    captured = {}

    def capturing_completion(system, user, *, max_tokens):
        captured["user"] = user
        return '{"claim": "user:1.plan = pro", "citations": ["ev-1"]}', {"in": 1, "out": 1}

    deriver = LLMDeriver(capturing_completion, model="test-model")
    candidate = _candidate(content="pro", excerpt="Alice's plan is pro.")

    deriver.derive("user:1.plan", NOW, [candidate], query_id="q1", derived_at=NOW, route="fresh")

    assert "Alice's plan is pro." in captured["user"]
    assert captured["user"].count("pro") == 1  # the excerpt's own "pro", not a second bare-content line


def test_prompt_falls_back_to_bare_content_when_no_excerpt():
    captured = {}

    def capturing_completion(system, user, *, max_tokens):
        captured["user"] = user
        return '{"claim": "user:1.plan = pro", "citations": ["ev-1"]}', {"in": 1, "out": 1}

    deriver = LLMDeriver(capturing_completion, model="test-model")

    deriver.derive("user:1.plan", NOW, [_candidate(content="pro", excerpt=None)], query_id="q1", derived_at=NOW, route="fresh")

    assert captured["user"].splitlines()[-1].endswith("pro")


def test_a_mix_of_real_and_hallucinated_citations_keeps_only_the_real_ones():
    completion = _QueuedCompletion(
        ('{"claim": "user:1.plan = pro", "citations": ["ev-1", "hallucinated"]}', {"in": 5, "out": 5}),
    )
    deriver = LLMDeriver(completion, model="test-model")

    assertion = deriver.derive("user:1.plan", NOW, [_candidate()], query_id="q1", derived_at=NOW, route="fresh")

    assert assertion.citations == ["ev-1"]


def test_completion_timeout_raises_deriver_timeout():
    def _timing_out(system, user, *, max_tokens):
        raise TimeoutError("simulated timeout")

    deriver = LLMDeriver(_timing_out, model="test-model", timeout_s=1.0)

    with pytest.raises(DeriverTimeout):
        deriver.derive("user:1.plan", NOW, [_candidate()], query_id="q1", derived_at=NOW, route="fresh")


def test_no_candidates_raises_uncited_assertion_without_calling_the_model():
    completion = _QueuedCompletion()  # would raise AssertionError if called
    deriver = LLMDeriver(completion, model="test-model")

    with pytest.raises(UncitedAssertion):
        deriver.derive("user:1.plan", NOW, [], query_id="q1", derived_at=NOW, route="fresh")

    assert completion.calls == 0


def test_duplicate_citation_ids_are_kept_as_is_not_deduplicated():
    completion = _QueuedCompletion(('{"claim": "user:1.plan = pro", "citations": ["ev-1", "ev-1"]}', {"in": 5, "out": 5}))
    deriver = LLMDeriver(completion, model="test-model")

    assertion = deriver.derive("user:1.plan", NOW, [_candidate()], query_id="q1", derived_at=NOW, route="fresh")

    assert assertion.citations == ["ev-1", "ev-1"]


def test_a_citations_field_that_is_a_bare_string_not_a_list_yields_no_valid_citations():
    # `parsed.get("citations") or []` keeps a truthy string as-is; iterating
    # a string yields its individual characters, none of which match a real
    # event id -- so this fails safely into UncitedAssertion rather than
    # silently misbehaving. Pinned down explicitly since it's a subtle
    # consequence of Python's string iteration, not an intentional check.
    completion = _QueuedCompletion(('{"claim": "user:1.plan = pro", "citations": "ev-1"}', {"in": 5, "out": 5}))
    deriver = LLMDeriver(completion, model="test-model")

    with pytest.raises(UncitedAssertion):
        deriver.derive("user:1.plan", NOW, [_candidate()], query_id="q1", derived_at=NOW, route="fresh")


def test_empty_string_claim_is_treated_the_same_as_a_missing_claim():
    completion = _QueuedCompletion(('{"claim": "", "citations": ["ev-1"]}', {"in": 5, "out": 5}))
    deriver = LLMDeriver(completion, model="test-model")

    with pytest.raises(UncitedAssertion):
        deriver.derive("user:1.plan", NOW, [_candidate()], query_id="q1", derived_at=NOW, route="fresh")


def test_a_brace_inside_a_quoted_string_value_does_not_break_extraction():
    text = 'Here is my answer: {"claim": "the value is {nested}", "citations": ["ev-1"]} - hope that helps!'
    completion = _QueuedCompletion((text, {"in": 5, "out": 5}))
    deriver = LLMDeriver(completion, model="test-model")

    assertion = deriver.derive("user:1.plan", NOW, [_candidate()], query_id="q1", derived_at=NOW, route="fresh")

    assert assertion.claim == "the value is {nested}"
    assert assertion.citations == ["ev-1"]


def test_json_wrapped_in_a_markdown_code_fence_extracts_on_the_first_try():
    fenced = '```json\n{"claim": "user:1.plan = pro", "citations": ["ev-1"]}\n```'
    completion = _QueuedCompletion((fenced, {"in": 5, "out": 5}))
    deriver = LLMDeriver(completion, model="test-model")

    assertion = deriver.derive("user:1.plan", NOW, [_candidate()], query_id="q1", derived_at=NOW, route="fresh")

    assert assertion.claim == "user:1.plan = pro"
    assert completion.calls == 1  # no repair retry needed
