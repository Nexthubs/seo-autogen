"""TieredLLMProvider tests: per-tier endpoints, routing, provenance.

Endpoint-split follow-up to docs/TASK-LLM-MODEL-TIERING.md:

  - model=None            -> writing tier (LLM_BASE_URL / LLM_API_KEY)
  - model=<explicit name> -> analysis tier (LLM_*_ANALYSIS, per-field
                             fallback to the writing tier when empty)
  - a second connection is built only when the analysis triple is distinct
  - usage / _last_model always describe the instance that served the call
"""

import json

import httpx
import pytest
from pydantic import BaseModel

from app.core.config import Settings
from app.providers.llm.tiering import TieredLLMProvider

FAST_BACKOFF = (0.0, 0.0, 0.0)
WRITER_URL = "http://writer.test/v1"
ANALYSIS_URL = "http://analysis.test/v1"


class Outline(BaseModel):
    title: str


def _settings(**overrides) -> Settings:
    base = dict(
        llm_base_url=WRITER_URL,
        llm_api_key="writer-key",
        llm_model="writer-model",
        _env_file=None,
    )
    base.update(overrides)
    return Settings(**base)


def _client(
    base_url: str, handler, api_key: str | None = None
) -> httpx.AsyncClient:
    # The production client carries the tier API key as a client-level
    # Authorization header (openai_compatible.__init__), so injected test
    # clients must do the same for the assertions to be meaningful.
    headers = (
        {"Authorization": f"Bearer {api_key}"} if api_key else {}
    )
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=base_url,
        headers=headers,
    )


def _chat_ok(content: str = json.dumps({"title": "t"})) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "cmpl-1",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


async def _closed(p) -> None:
    await p.aclose()


# ---------------------------------------------------------------------
# Connection layout
# ---------------------------------------------------------------------
async def test_single_endpoint_when_analysis_unset():
    """No *_ANALYSIS vars -> one provider only, shared by both tiers."""
    p = TieredLLMProvider(settings=_settings())
    assert p._analysis is None
    assert p._writing is not None
    await p.aclose()


async def test_second_endpoint_when_model_only_differs():
    """A distinct analysis model alone still builds a second provider."""
    p = TieredLLMProvider(
        settings=_settings(llm_model_analysis="analysis-model")
    )
    assert p._analysis is not None
    await p.aclose()


# ---------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------
async def test_routes_writing_and_analysis_to_separate_endpoints():
    seen = {"writing": [], "analysis": []}

    def writing_handler(request: httpx.Request) -> httpx.Response:
        seen["writing"].append(
            (str(request.url), request.headers.get("authorization"))
        )
        return _chat_ok()

    def analysis_handler(request: httpx.Request) -> httpx.Response:
        seen["analysis"].append(
            (str(request.url), request.headers.get("authorization"))
        )
        return _chat_ok()

    p = TieredLLMProvider(
        settings=_settings(
            llm_model_analysis="analysis-model",
            llm_base_url_analysis=ANALYSIS_URL,
            llm_api_key_analysis="analysis-key",
        ),
        writing_client=_client(WRITER_URL, writing_handler, "writer-key"),
        analysis_client=_client(ANALYSIS_URL, analysis_handler, "analysis-key"),
    )
    try:
        # Writing tier: model=None.
        await p.generate_structured(
            system_prompt="s", user_prompt="u", response_model=Outline
        )
        # Analysis tier: explicit model (what the 14 step call sites pass).
        await p.generate_structured(
            system_prompt="s",
            user_prompt="u",
            response_model=Outline,
            model="analysis-model",
        )
        # generate_text has no model parameter: always the writing tier.
        await p.generate_text(system_prompt="s", user_prompt="u")
    finally:
        await p.aclose()

    assert len(seen["writing"]) == 2
    assert len(seen["analysis"]) == 1
    # Each tier hits its own endpoint with its own API key.
    assert all(
        url.startswith(WRITER_URL) and auth == "Bearer writer-key"
        for url, auth in seen["writing"]
    )
    assert (
        seen["analysis"][0][0].startswith(ANALYSIS_URL)
        and seen["analysis"][0][1] == "Bearer analysis-key"
    )


async def test_per_field_fallback_for_analysis_endpoint():
    """Only LLM_API_KEY_ANALYSIS set -> analysis reuses the writing
    base URL but sends the analysis key (second connection required)."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        return _chat_ok()

    p = TieredLLMProvider(
        settings=_settings(llm_api_key_analysis="analysis-key"),
        writing_client=_client(WRITER_URL, handler, "writer-key"),
        analysis_client=_client(WRITER_URL, handler, "analysis-key"),
    )
    try:
        await p.generate_structured(
            system_prompt="s", user_prompt="u", response_model=Outline
        )
        await p.generate_structured(
            system_prompt="s",
            user_prompt="u",
            response_model=Outline,
            model="writer-model",  # effective analysis model = llm_model
        )
    finally:
        await p.aclose()

    assert len(seen) == 2
    assert seen[0][0].startswith(WRITER_URL) and seen[0][1] == "Bearer writer-key"
    # Same base URL, but the analysis-tier API key.
    assert seen[1][0].startswith(WRITER_URL) and seen[1][1] == "Bearer analysis-key"


async def test_explicit_model_override_still_reaches_analysis_tier():
    """A model override different from both tier defaults still routes to
    the analysis provider and the explicit name is sent."""
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body["model"])
        return _chat_ok()

    p = TieredLLMProvider(
        settings=_settings(llm_model_analysis="analysis-model"),
        writing_client=_client(WRITER_URL, handler),
        analysis_client=_client(WRITER_URL, handler),
    )
    try:
        await p.generate_structured(
            system_prompt="s",
            user_prompt="u",
            response_model=Outline,
            model="analysis-model",
        )
    finally:
        await p.aclose()
    assert sent == ["analysis-model"]


# ---------------------------------------------------------------------
# Provenance / metering
# ---------------------------------------------------------------------
async def test_last_model_reflects_last_served_tier():
    p = TieredLLMProvider(
        settings=_settings(llm_model_analysis="analysis-model"),
        writing_client=_client(WRITER_URL, lambda r: _chat_ok()),
        analysis_client=_client(WRITER_URL, lambda r: _chat_ok()),
    )
    try:
        assert p._last_model is None
        await p.generate_structured(
            system_prompt="s", user_prompt="u", response_model=Outline
        )
        assert p._last_model == "writer-model"
        await p.generate_structured(
            system_prompt="s",
            user_prompt="u",
            response_model=Outline,
            model="analysis-model",
        )
        assert p._last_model == "analysis-model"
    finally:
        await p.aclose()


async def test_begin_usage_resets_both_instances_take_reads_served():
    p = TieredLLMProvider(
        settings=_settings(llm_model_analysis="analysis-model"),
        writing_client=_client(WRITER_URL, lambda r: _chat_ok()),
        analysis_client=_client(WRITER_URL, lambda r: _chat_ok()),
    )
    try:
        p.begin_usage()
        await p.generate_structured(
            system_prompt="s", user_prompt="u", response_model=Outline
        )
        assert p.take_usage() == (10, 5)
        # A stale usage on the other instance must not leak into the next
        # reading: begin_usage resets every instance.
        p.begin_usage()
        assert p.take_usage() is None
    finally:
        await p.aclose()


async def test_single_provider_usage_sharing():
    """Cheap path (no *_ANALYSIS vars): the step call sites pass an
    explicit model (= llm_model) which still lands on the shared provider;
    usage is reset per logical call."""
    p = TieredLLMProvider(
        settings=_settings(),
        writing_client=_client(WRITER_URL, lambda r: _chat_ok()),
    )
    try:
        p.begin_usage()
        await p.generate_structured(
            system_prompt="s", user_prompt="u", response_model=Outline
        )
        assert p.take_usage() == (10, 5)
        p.begin_usage()
        # Effective analysis model with no LLM_MODEL_ANALYSIS = llm_model.
        await p.generate_structured(
            system_prompt="s",
            user_prompt="u",
            response_model=Outline,
            model="writer-model",
        )
        assert p.take_usage() == (10, 5)
        assert p._last_model == "writer-model"
    finally:
        await p.aclose()


# ---------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------
async def test_health_check_covers_every_configured_endpoint():
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    def dead(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    good = TieredLLMProvider(
        settings=_settings(llm_model_analysis="analysis-model"),
        writing_client=_client(WRITER_URL, ok),
        analysis_client=_client(WRITER_URL, ok),
    )
    assert await good.health_check() is True
    await good.aclose()

    bad = TieredLLMProvider(
        settings=_settings(llm_model_analysis="analysis-model"),
        writing_client=_client(WRITER_URL, ok),
        analysis_client=_client(WRITER_URL, dead),
    )
    assert await bad.health_check() is False
    await bad.aclose()

    single = TieredLLMProvider(
        settings=_settings(), writing_client=_client(WRITER_URL, ok)
    )
    assert await single.health_check() is True
    await single.aclose()


async def test_aclose_closes_every_owned_client(monkeypatch):
    """Self-built clients (no injection) are closed for both tiers."""
    closed = []
    real_client = httpx.AsyncClient

    def make_client(*args, **kwargs) -> httpx.AsyncClient:
        client = real_client(
            transport=httpx.MockTransport(lambda r: _chat_ok()),
            **{k: v for k, v in kwargs.items() if k != "transport"},
        )
        original_close = client.aclose

        async def tracked() -> None:
            closed.append(client)
            await original_close()

        client.aclose = tracked
        return client

    monkeypatch.setattr(
        "app.providers.llm.openai_compatible.httpx.AsyncClient", make_client
    )
    p = TieredLLMProvider(
        settings=_settings(
            llm_model_analysis="analysis-model",
            llm_base_url_analysis=ANALYSIS_URL,
            llm_api_key_analysis="analysis-key",
        )
    )
    assert p._analysis is not None
    await p.aclose()
    assert len(closed) == 2


async def test_aclose_leaves_injected_clients_to_their_owner():
    """Injected clients are owned by the caller; aclose must not close
    them (the provider only closes clients it built itself)."""
    closed = []
    injected = _client(WRITER_URL, lambda r: _chat_ok())
    original_close = injected.aclose

    async def tracked() -> None:
        closed.append(injected)
        await original_close()

    injected.aclose = tracked
    p = TieredLLMProvider(settings=_settings(), writing_client=injected)
    await p.aclose()
    assert closed == []
