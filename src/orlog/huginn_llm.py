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

from orlog.huginn import (
    Assertion,
    DeriverFailure,
    DeriverInsufficientEvidence,
    DeriverProviderError,
    DeriverTimeout,
    DeriverTruncated,
    UncitedAssertion,
)
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

        # Everything below treats `parsed` as untrusted: it is whatever JSON
        # the model emitted, and the shapes it gets wrong are not exotic.
        # `claim` was passed straight to Assertion(), so a model answering
        # {"claim": {"text": ...}} or {"claim": 42} raised a pydantic
        # ValidationError; `cid in valid_ids` assumed hashability, so
        # {"citations": [{"id": "ev-1"}]} raised TypeError: unhashable type.
        # Both escaped DeriverFailure and therefore escaped answer()
        # entirely, crashing the recall tool instead of abstaining.
        claim = parsed.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            raise UncitedAssertion(
                f"model returned a non-string claim ({type(claim).__name__}) for query_id={query_id!r}"
            )

        raw_citations = parsed.get("citations")
        if not isinstance(raw_citations, list):
            raise UncitedAssertion(
                f"model returned a non-list citations field ({type(raw_citations).__name__}) "
                f"for query_id={query_id!r}"
            )
        valid_ids = {c.event_id for c in candidates}
        citations = [cid for cid in raw_citations if isinstance(cid, str) and cid in valid_ids]

        if not citations:
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
        except DeriverFailure:
            raise
        except Exception as exc:
            # The adapters normalize a timeout and nothing else, so every
            # other provider condition -- 429 rate limit, 529 overloaded,
            # 401, a connection drop, a malformed response object -- used to
            # travel out of the deriver as a raw SDK exception. spec §A4 P3
            # requires a deriver-level failure to become an abstention, and
            # the SDK message can carry request context that has no business
            # in a client-facing error, so it is deliberately not echoed.
            raise DeriverProviderError(
                f"{type(exc).__name__} from the model provider for query_id={query_id!r}"
            ) from exc


#: Anthropic model families that still accept sampling parameters
#: (`temperature`/`top_p`/`top_k`) on the wire.
#:
#: The reasoning-model generations REMOVED sampling control and reject it
#: with a 400: Fable 5/5.1, Mythos 5/5.1, Opus 5, Opus 4.8, Opus 4.7 and
#: Sonnet 5 are all in that group. Older families (Haiku 4.5 -- orlog's
#: default -- Opus 4.6, Sonnet 4.6, and the 3.x/4.x line before them) still
#: honour it.
#:
#: The gate is an ALLOW-list rather than a deny-list, deliberately, because
#: the two ways of being wrong are not symmetric: sending `temperature` to a
#: model that rejects it 400s EVERY derive, so a stale deny-list breaks the
#: deriver outright the day a new model ships. Omitting it from a model that
#: would have accepted it costs only a little determinism. When in doubt,
#: don't send it.
_SAMPLING_PARAM_PREFIXES = (
    "claude-haiku-4-5",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-opus-4-5",
    "claude-sonnet-4-5",
    "claude-haiku-3",
    "claude-opus-3",
    "claude-sonnet-3",
    "claude-3",
)


def _accepts_sampling_params(model: str) -> bool:
    return model.startswith(_SAMPLING_PARAM_PREFIXES)


class AnthropicCompletion:
    """Adapts the `anthropic` SDK to CompletionFn. Temperature is pinned to 0
    (spec §B4 MUST) on every model that still has a temperature to pin --
    reproducibility is the point. On the reasoning models that removed
    sampling control it is omitted rather than forced, since sending it
    would 400 the request instead of making it more deterministic (those
    models are already deterministic-by-default at a fixed effort).
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

        # Sent via extra_body, not as a named argument: anthropic 1.x dropped
        # `temperature` from messages.create()'s signature, so the named form
        # raises TypeError on every call. Out-of-band it still reaches the
        # wire -- but only send it to a model that accepts one at all, or the
        # escape hatch just trades a TypeError for a 400 (see
        # _SAMPLING_PARAM_PREFIXES).
        extra_body = {"temperature": 0} if _accepts_sampling_params(self.model) else {}
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                extra_body=extra_body,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.APITimeoutError as exc:
            raise TimeoutError(str(exc)) from exc

        # A response cut off at max_tokens is a truncated JSON object, not an
        # answer. Left unchecked it fell through to _extract_json, failed,
        # burned the one repair retry at the SAME cap, truncated identically,
        # and abstained with "not valid JSON even after one repair retry" --
        # a misleading reason and two paid calls, on every future attempt.
        if response.stop_reason == "max_tokens":
            raise DeriverTruncated(
                f"model response hit the {max_tokens}-token cap before completing its JSON object"
            )

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

        if response.choices[0].finish_reason == "length":
            raise DeriverTruncated(
                f"model response hit the {max_tokens}-token cap before completing its JSON object"
            )

        text = response.choices[0].message.content or ""
        usage = response.usage
        return text, {"in": usage.prompt_tokens, "out": usage.completion_tokens}
