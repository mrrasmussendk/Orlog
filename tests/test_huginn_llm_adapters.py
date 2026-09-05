"""Contract: AnthropicCompletion/OpenAICompletion -- the adapters that
actually build the request sent to a real model. Distinct from
test_huginn_llm.py, which tests LLMDeriver's prompt-contract logic against
a fake CompletionFn and never touches either SDK. Every test here
monkeypatches the SDK's client class itself, so nothing here ever makes a
network call or needs a real API key.
"""

import pytest

from orlog.huginn_llm import AnthropicCompletion, OpenAICompletion

httpx = pytest.importorskip("httpx")


# -- Anthropic --


class _FakeAnthropicBlock:
    def __init__(self, type_, text=None):
        self.type = type_
        self.text = text


class _FakeAnthropicUsage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeAnthropicResponse:
    def __init__(self, content, usage, stop_reason="end_turn"):
        self.content = content
        self.usage = usage
        self.stop_reason = stop_reason


class _FakeAnthropicMessages:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


class _FakeAnthropicClient:
    def __init__(self, *, response=None, error=None):
        self.messages = _FakeAnthropicMessages(response=response, error=error)


def _install_fake_anthropic_client(monkeypatch, fake_client):
    monkeypatch.setattr("anthropic.Anthropic", lambda *a, **k: fake_client)


def test_anthropic_completion_requires_the_api_key_env_var(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(RuntimeError):
        AnthropicCompletion(model="test-model")


def test_anthropic_completion_joins_text_across_multiple_blocks_and_skips_non_text(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    response = _FakeAnthropicResponse(
        content=[
            _FakeAnthropicBlock("text", "hello "),
            _FakeAnthropicBlock("tool_use"),  # no .text -- must never be concatenated
            _FakeAnthropicBlock("text", "world"),
        ],
        usage=_FakeAnthropicUsage(10, 5),
    )
    fake_client = _FakeAnthropicClient(response=response)
    _install_fake_anthropic_client(monkeypatch, fake_client)

    completion = AnthropicCompletion(model="test-model")
    text, usage = completion("system prompt", "user prompt", max_tokens=100)

    assert text == "hello world"
    assert usage == {"in": 10, "out": 5}


def test_anthropic_completion_passes_temperature_zero_and_the_configured_model(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    response = _FakeAnthropicResponse(content=[_FakeAnthropicBlock("text", "x")], usage=_FakeAnthropicUsage(1, 1))
    fake_client = _FakeAnthropicClient(response=response)
    _install_fake_anthropic_client(monkeypatch, fake_client)

    # orlog's default model, and one that still accepts a sampling param.
    completion = AnthropicCompletion(model="claude-haiku-4-5")
    completion("sys", "user", max_tokens=42)

    call = fake_client.messages.calls[0]
    assert call["model"] == "claude-haiku-4-5"
    # extra_body, not a named kwarg -- see AnthropicCompletion.__call__.
    assert call["extra_body"] == {"temperature": 0}
    assert call["max_tokens"] == 42
    assert call["system"] == "sys"
    assert call["messages"] == [{"role": "user", "content": "user"}]


def test_anthropic_completion_omits_temperature_on_models_that_reject_sampling_params(monkeypatch):
    # The reasoning generations removed sampling control and 400 on
    # `temperature`. Sending it anyway -- which the extra_body escape hatch
    # did unconditionally -- would fail EVERY derive for anyone who
    # configured one of these in orlog.toml, since DeriverConfig.model is
    # free-form.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    for model in ("claude-opus-5", "claude-sonnet-5", "claude-opus-4-7", "claude-fable-5-1"):
        response = _FakeAnthropicResponse(content=[_FakeAnthropicBlock("text", "x")], usage=_FakeAnthropicUsage(1, 1))
        fake_client = _FakeAnthropicClient(response=response)
        _install_fake_anthropic_client(monkeypatch, fake_client)

        AnthropicCompletion(model=model)("sys", "user", max_tokens=42)

        assert fake_client.messages.calls[0]["extra_body"] == {}, model


def test_anthropic_completion_omits_temperature_for_an_unrecognized_model(monkeypatch):
    # Unknown model ids default to omitting it: a model that would have
    # accepted temperature loses only a little determinism, whereas one that
    # rejects it 400s every single call.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    response = _FakeAnthropicResponse(content=[_FakeAnthropicBlock("text", "x")], usage=_FakeAnthropicUsage(1, 1))
    fake_client = _FakeAnthropicClient(response=response)
    _install_fake_anthropic_client(monkeypatch, fake_client)

    AnthropicCompletion(model="some-model-released-next-year")("sys", "user", max_tokens=42)

    assert fake_client.messages.calls[0]["extra_body"] == {}


def test_anthropic_completion_reports_a_max_tokens_truncation_instead_of_parsing_it(monkeypatch):
    # A truncated response is an incomplete JSON object. Treated as a normal
    # answer it failed to parse, burned the one repair retry at the same
    # cap, truncated identically, and abstained with a reason that named
    # JSON validity rather than the actual cause.
    from orlog.huginn import DeriverTruncated

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    response = _FakeAnthropicResponse(
        content=[_FakeAnthropicBlock("text", '{"claim": "user:1.plan = pr')],
        usage=_FakeAnthropicUsage(10, 42),
        stop_reason="max_tokens",
    )
    fake_client = _FakeAnthropicClient(response=response)
    _install_fake_anthropic_client(monkeypatch, fake_client)

    with pytest.raises(DeriverTruncated):
        AnthropicCompletion(model="claude-haiku-4-5")("sys", "user", max_tokens=42)


def test_anthropic_completion_converts_the_providers_timeout_into_a_plain_timeouterror(monkeypatch):
    import anthropic

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    request = httpx.Request("POST", "https://example.com")
    fake_client = _FakeAnthropicClient(error=anthropic.APITimeoutError(request=request))
    _install_fake_anthropic_client(monkeypatch, fake_client)

    completion = AnthropicCompletion(model="test-model")
    with pytest.raises(TimeoutError):
        completion("sys", "user", max_tokens=10)


# -- OpenAI --


class _FakeOpenAIMessage:
    def __init__(self, content):
        self.content = content


class _FakeOpenAIChoice:
    def __init__(self, content, finish_reason="stop"):
        self.message = _FakeOpenAIMessage(content)
        self.finish_reason = finish_reason


class _FakeOpenAIUsage:
    def __init__(self, prompt_tokens, completion_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeOpenAIResponse:
    def __init__(self, content, usage, finish_reason="stop"):
        self.choices = [_FakeOpenAIChoice(content, finish_reason)]
        self.usage = usage


class _FakeOpenAICompletions:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


class _FakeOpenAIChat:
    def __init__(self, response=None, error=None):
        self.completions = _FakeOpenAICompletions(response=response, error=error)


class _FakeOpenAIClient:
    def __init__(self, *, response=None, error=None):
        self.chat = _FakeOpenAIChat(response=response, error=error)


def _install_fake_openai_client(monkeypatch, fake_client):
    monkeypatch.setattr("openai.OpenAI", lambda *a, **k: fake_client)


def test_openai_completion_requires_the_api_key_env_var(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(RuntimeError):
        OpenAICompletion(model="test-model")


def test_openai_completion_reads_text_and_usage_from_the_chat_completion_shape(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    response = _FakeOpenAIResponse(content="hi there", usage=_FakeOpenAIUsage(7, 3))
    fake_client = _FakeOpenAIClient(response=response)
    _install_fake_openai_client(monkeypatch, fake_client)

    completion = OpenAICompletion(model="test-model")
    text, usage = completion("sys", "user", max_tokens=50)

    assert text == "hi there"
    assert usage == {"in": 7, "out": 3}


def test_openai_completion_converts_the_providers_timeout_into_a_plain_timeouterror(monkeypatch):
    import openai

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    request = httpx.Request("POST", "https://example.com")
    fake_client = _FakeOpenAIClient(error=openai.APITimeoutError(request=request))
    _install_fake_openai_client(monkeypatch, fake_client)

    completion = OpenAICompletion(model="test-model")
    with pytest.raises(TimeoutError):
        completion("sys", "user", max_tokens=10)


def test_openai_completion_reports_a_length_truncation_instead_of_parsing_it(monkeypatch):
    from orlog.huginn import DeriverTruncated

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    response = _FakeOpenAIResponse(content='{"claim": "user:1.pl', usage=_FakeOpenAIUsage(7, 3), finish_reason="length")
    fake_client = _FakeOpenAIClient(response=response)
    _install_fake_openai_client(monkeypatch, fake_client)

    with pytest.raises(DeriverTruncated):
        OpenAICompletion(model="gpt-test")("sys", "user", max_tokens=50)
