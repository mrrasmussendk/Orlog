"""huginn_llm: the real, model-backed Deriver (ORLOG-SPEC.md §B4).

Design decisions:

1. `LLMDeriver` depends on a `CompletionFn` protocol -- (system, user) ->
   (text, token_usage) -- not directly on the anthropic/openai SDKs. This
   is what "provider-agnostic over anthropic/openai-compatible messages
   APIs" means in practice: swapping providers means swapping the
   CompletionFn, never touching the prompt contract, JSON-repair logic, or
   failure taxonomy below. Tests inject a trivial stand-in CompletionFn
   instead of making a real network call -- this file has NO test suite
   entry that requires an API key.

2. Timeouts are NOT implemented with signals or threads here. Each
   CompletionFn adapter (AnthropicCompletion, OpenAICompletion) configures
   its own client-level timeout and converts the provider's own timeout
   exception into a plain stdlib TimeoutError; LLMDeriver only ever has to
   know about that one, provider-neutral signal.

3. Every MUST clause from spec §B4 lives in derive(), in order: temperature
   0 (the CompletionFn adapters' job -- not configurable, since
   reproducibility is the point); robust JSON extraction (largest {...}
   span); INSUFFICIENT -> DeriverInsufficientEvidence; malformed JSON -> one
   repair retry, then UncitedAssertion; citations filtered to provided
   candidate ids, unknown ids dropped; none remaining -> UncitedAssertion;
   tokens/latency recorded on the Assertion.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Protocol

from orlog.huginn import Assertion, DeriverInsufficientEvidence, DeriverTimeout, UncitedAssertion
from orlog.retrieval import Candidate

INSUFFICIENT_TOKEN = "INSUFFICIENT"

SYSTEM_PROMPT = (
    "Answer ONLY from the numbered facts provided below. Cite the fact ids "
    "that support your answer. If the facts do not contain the answer, "
    f"output exactly the token {INSUFFICIENT_TOKEN} and nothing else. "
    'Otherwise respond with JSON only, matching '
    '{"claim": string, "citations": [event_id, ...]}. '
    'The "claim" string MUST include the exact question text given after '
    '"Question:" verbatim, together with the answer value, in the form '
    '"<question> = <value>" -- e.g. if the question is "X" and the answer '
    'value is "Y", respond {"claim": "X = Y", "citations": [...]}.'
)


def _render_facts(candidates: list[Candidate]) -> str:
    # Prefer excerpt (the remembered evidence sentence) over the bare
    # content value -- a real model reading just a bare value with no
    # attribute label or grounding text reliably answers INSUFFICIENT even
    # on orlog's own README example (see retrieval.py's Candidate.excerpt
    # docstring); ScriptedDeriver, unaffected by this function, still uses
    # content directly.
    lines = [f"[{c.event_id}] ({c.valid_from.date().isoformat()}) {c.excerpt or c.content}" for c in candidates]
    return "\n".join(lines)


def _extract_json(text: str) -> dict | None:
    """The largest {...} span in the text (spec §B4 MUST) -- models
    sometimes wrap JSON in prose or a code fence.
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


class CompletionFn(Protocol):
    def __call__(self, system: str, user: str, *, max_tokens: int) -> tuple[str, dict]: ...


class LLMDeriver:
    """A real Deriver, provider-agnostic over any CompletionFn."""

    def __init__(self, completion_fn: CompletionFn, *, model: str, max_tokens: int = 400, timeout_s: float = 30.0) -> None:
        self._complete = completion_fn
        self.model = model
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s

    def derive(
        self,
        query: str,
        as_of: datetime,
        candidates: list[Candidate],
        *,
        query_id: str,
        derived_at: datetime,
        route: str,
    ) -> Assertion:
        if not candidates:
            raise UncitedAssertion(f"no candidates to derive an assertion from for query_id={query_id!r}")

        started = time.monotonic()
        user_prompt = f"Question: {query}\n\nFacts:\n{_render_facts(candidates)}"

        raw, usage = self._call(user_prompt, query_id)
        if raw.strip() == INSUFFICIENT_TOKEN:
            raise DeriverInsufficientEvidence(f"model found no sufficient evidence for query_id={query_id!r}")

        parsed = _extract_json(raw)
        if parsed is None:
            # ONE repair retry (spec §B4 MUST), with an error-explaining message.
            repair_prompt = (
                f"{user_prompt}\n\nYour previous response was not valid JSON: {raw!r}. "
                'Respond again with ONLY the JSON object {"claim": string, "citations": [event_id, ...]}.'
            )
            raw, usage2 = self._call(repair_prompt, query_id)
            usage = {"in": usage["in"] + usage2["in"], "out": usage["out"] + usage2["out"]}
            if raw.strip() == INSUFFICIENT_TOKEN:
                raise DeriverInsufficientEvidence(f"model found no sufficient evidence for query_id={query_id!r} (on repair)")
            parsed = _extract_json(raw)
            if parsed is None:
                raise UncitedAssertion(f"model output was not valid JSON even after one repair retry for query_id={query_id!r}")

        claim = parsed.get("claim")
        valid_ids = {c.event_id for c in candidates}
        citations = [cid for cid in (parsed.get("citations") or []) if cid in valid_ids]

        if not claim or not citations:
            raise UncitedAssertion(f"model produced no valid citations for query_id={query_id!r}")

        return Assertion(
            query_id=query_id,
            claim=claim,
            citations=citations,
            derived_by=self.model,
            derived_at=derived_at,
            route=route,
            tokens=usage,
            latency_ms=(time.monotonic() - started) * 1000,
        )

    def _call(self, user_prompt: str, query_id: str) -> tuple[str, dict]:
        try:
            return self._complete(SYSTEM_PROMPT, user_prompt, max_tokens=self.max_tokens)
        except TimeoutError as exc:
            raise DeriverTimeout(f"model call timed out after {self.timeout_s}s for query_id={query_id!r}") from exc


class AnthropicCompletion:
    """Adapts the `anthropic` SDK to CompletionFn. Temperature is always 0
    (spec §B4 MUST) -- not configurable, reproducibility is the point.
    """

    def __init__(self, *, model: str, api_key_env: str = "ANTHROPIC_API_KEY", timeout_s: float = 30.0) -> None:
        import anthropic

        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise RuntimeError(f"{api_key_env} is not set -- cannot construct an AnthropicCompletion")
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout_s)
        self.model = model

    def __call__(self, system: str, user: str, *, max_tokens: int) -> tuple[str, dict]:
        import anthropic

        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=0,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.APITimeoutError as exc:
            raise TimeoutError(str(exc)) from exc

        text = "".join(block.text for block in response.content if block.type == "text")
        return text, {"in": response.usage.input_tokens, "out": response.usage.output_tokens}


class OpenAICompletion:
    """Adapts the `openai` SDK to CompletionFn -- the other spec §B4
    "openai-compatible messages API" option.
    """

    def __init__(self, *, model: str, api_key_env: str = "OPENAI_API_KEY", timeout_s: float = 30.0) -> None:
        import openai

        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise RuntimeError(f"{api_key_env} is not set -- cannot construct an OpenAICompletion")
        self._client = openai.OpenAI(api_key=api_key, timeout=timeout_s)
        self.model = model

    def __call__(self, system: str, user: str, *, max_tokens: int) -> tuple[str, dict]:
        import openai

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=0,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
        except openai.APITimeoutError as exc:
            raise TimeoutError(str(exc)) from exc

        text = response.choices[0].message.content or ""
        usage = response.usage
        return text, {"in": usage.prompt_tokens, "out": usage.completion_tokens}
