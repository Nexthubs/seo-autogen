"""P1 LLM provider tests via httpx.MockTransport (spec sections 10.1, 49, 51).

Covers:
  - generate_text happy path (the "Return hello" acceptance shape)
  - generate_structured: success, repair after invalid JSON, failure
    after 2 repairs (LLM_STRUCTURED_OUTPUT_INVALID + raw output)
  - retry policy: 429/500 retried with backoff, 401 not retried,
    exhaustion raises LLM_UNAVAILABLE
  - health_check
  - JSON extraction helper
"""

import json

import httpx
import pytest
from pydantic import BaseModel

from app.core.config import Settings
from app.core.exceptions import ErrorCode, PipelineError
from app.providers.llm.openai_compatible import (
    MAX_STRUCTURED_REPAIRS,
    OpenAICompatibleLLMProvider,
    _extract_json_object,
)

FAST_BACKOFF = (0.001, 0.001, 0.001)


def _settings() -> Settings:
    return Settings(
        llm_base_url="http://llm.test/v1",
        llm_api_key="test-key",
        llm_model="test-model",
        llm_max_retries=2,
        _env_file=None,
    )


def _provider(handler) -> OpenAICompatibleLLMProvider:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://llm.test/v1")
    return OpenAICompatibleLLMProvider(
        settings=_settings(),
        client=client,
        backoff_seconds=FAST_BACKOFF,
    )


def _chat_ok(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "cmpl-1",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def _status_response(status: int, body: str = "err") -> httpx.Response:
    return httpx.Response(status, text=body)


# ----------------------------------------------------------------------
# generate_text
# ----------------------------------------------------------------------
async def test_generate_text_returns_hello():
    """The P1 acceptance shape: LLM 'Return hello' -> 'hello'."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _chat_ok("hello")

    provider = _provider(handler)
    out = await provider.generate_text(
        system_prompt="Reply in one word.",
        user_prompt="Return hello",
    )
    assert out == "hello"
    # POST to the OpenAI-compatible chat completions endpoint
    assert calls[0].url.path == "/v1/chat/completions"
    payload = json.loads(calls[0].content)
    assert payload["model"] == "test-model"
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]
    await provider.aclose()


async def test_provider_client_carries_bearer_auth():
    """The self-created client must send the configured API key."""
    provider = OpenAICompatibleLLMProvider(settings=_settings())
    try:
        assert provider._client.headers["Authorization"] == "Bearer test-key"
    finally:
        await provider.aclose()


async def test_generate_text_default_temperature():
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return _chat_ok("ok")

    provider = _provider(handler)
    await provider.generate_text(system_prompt="s", user_prompt="u")
    assert captured[0]["temperature"] == 0.25
    await provider.aclose()


# ----------------------------------------------------------------------
# generate_structured
# ----------------------------------------------------------------------
class Greeting(BaseModel):
    message: str
    count: int


async def test_structured_output_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_ok('{"message": "hi", "count": 2}')

    provider = _provider(handler)
    out = await provider.generate_structured(
        system_prompt="s", user_prompt="u", response_model=Greeting
    )
    assert isinstance(out, Greeting)
    assert out.count == 2
    await provider.aclose()


async def test_structured_output_with_code_fence_and_commentary():
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_ok(
            "Sure! Here is the JSON:\n```json\n"
            '{"message": "hi", "count": 1}\n```\nHope that helps.'
        )

    provider = _provider(handler)
    out = await provider.generate_structured(
        system_prompt="s", user_prompt="u", response_model=Greeting
    )
    assert out.message == "hi"
    await provider.aclose()


async def test_structured_output_repairs_after_invalid_json():
    """Spec section 49: one bad output, repair prompt, second output ok."""
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        body = json.loads(request.content)
        user_prompt = body["messages"][1]["content"]
        if state["n"] == 1:
            return _chat_ok("not json at all")
        # Second call must carry the repair context.
        assert "Previous response" in user_prompt
        assert "not json at all" in user_prompt
        return _chat_ok('{"message": "fixed", "count": 9}')

    provider = _provider(handler)
    out = await provider.generate_structured(
        system_prompt="s", user_prompt="u", response_model=Greeting
    )
    assert out.count == 9
    assert state["n"] == 2
    await provider.aclose()


async def test_structured_output_repair_twice_then_success():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] <= MAX_STRUCTURED_REPAIRS:
            return _chat_ok("{invalid")
        return _chat_ok('{"message": "ok", "count": 1}')

    provider = _provider(handler)
    out = await provider.generate_structured(
        system_prompt="s", user_prompt="u", response_model=Greeting
    )
    assert out.message == "ok"
    assert state["n"] == MAX_STRUCTURED_REPAIRS + 1
    await provider.aclose()


async def test_structured_output_failure_after_two_repairs():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        # valid JSON shape, but wrong fields -> validation error each time
        return _chat_ok('{"wrong_field": true}')

    provider = _provider(handler)
    with pytest.raises(PipelineError) as excinfo:
        await provider.generate_structured(
            system_prompt="s", user_prompt="u", response_model=Greeting
        )
    err = excinfo.value
    assert err.error_code is ErrorCode.LLM_STRUCTURED_OUTPUT_INVALID
    assert err.raw is not None
    assert "wrong_field" in err.raw
    # 1 initial call + 2 repairs
    assert state["n"] == MAX_STRUCTURED_REPAIRS + 1
    await provider.aclose()


# ----------------------------------------------------------------------
# retry policy (spec section 51)
# ----------------------------------------------------------------------
async def test_retry_on_429_then_success():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return _status_response(429, "rate limited")
        return _chat_ok("ok")

    provider = _provider(handler)
    out = await provider.generate_text(system_prompt="s", user_prompt="u")
    assert out == "ok"
    assert state["n"] == 2
    await provider.aclose()


async def test_retry_on_500_then_success():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return _status_response(500, "boom")
        return _chat_ok("ok")

    provider = _provider(handler)
    out = await provider.generate_text(system_prompt="s", user_prompt="u")
    assert out == "ok"
    assert state["n"] == 2
    await provider.aclose()


async def test_no_retry_on_401():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return _status_response(401, "unauthorized")

    provider = _provider(handler)
    with pytest.raises(PipelineError) as excinfo:
        await provider.generate_text(system_prompt="s", user_prompt="u")
    assert excinfo.value.error_code is ErrorCode.LLM_UNAVAILABLE
    assert state["n"] == 1  # no retries for auth errors
    await provider.aclose()


async def test_no_retry_on_400():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return _status_response(400, "bad request")

    provider = _provider(handler)
    with pytest.raises(PipelineError):
        await provider.generate_text(system_prompt="s", user_prompt="u")
    assert state["n"] == 1
    await provider.aclose()


async def test_retry_exhaustion_raises_llm_unavailable():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return _status_response(503, "unavailable")

    provider = _provider(handler)
    with pytest.raises(PipelineError) as excinfo:
        await provider.generate_text(system_prompt="s", user_prompt="u")
    assert excinfo.value.error_code is ErrorCode.LLM_UNAVAILABLE
    # 3 attempts = 1 + 2 retries
    assert state["n"] == 3
    await provider.aclose()


async def test_retry_on_timeout_then_success():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ConnectError("connection reset")
        return _chat_ok("ok")

    provider = _provider(handler)
    out = await provider.generate_text(system_prompt="s", user_prompt="u")
    assert out == "ok"
    assert state["n"] == 2
    await provider.aclose()


# ----------------------------------------------------------------------
# health_check
# ----------------------------------------------------------------------
async def test_health_check_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    provider = _provider(handler)
    assert await provider.health_check() is True
    await provider.aclose()


async def test_health_check_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    provider = _provider(handler)
    assert await provider.health_check() is False
    await provider.aclose()


# ----------------------------------------------------------------------
# JSON extraction helper
# ----------------------------------------------------------------------
def test_extract_json_plain():
    assert _extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_json_nested_with_commentary():
    out = _extract_json_object('here: {"a": {"b": 2}, "s": "x{y}"} done')
    assert out == {"a": {"b": 2}, "s": "x{y}"}


def test_extract_json_no_object():
    with pytest.raises(ValueError):
        _extract_json_object("no braces here")


def test_extract_json_unbalanced():
    with pytest.raises(ValueError):
        _extract_json_object('{"a": 1')
