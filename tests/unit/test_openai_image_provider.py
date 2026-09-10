"""P6 unit: OpenAIImageProvider (spec sections 2.4, 10.4, 34, 51).

httpx.MockTransport — no network. Verifies the request shape,
b64/url payloads, retry policy (2 retries, retryable set), local
storage (section 34) and health_check behavior.
"""

import base64

import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import ErrorCode, PipelineError
from app.providers.image.openai_image import OpenAIImageProvider
from app.schemas.images import ImageGenerationRequest

FAST_BACKOFF = (0.001, 0.001, 0.001)

#: 1x1 transparent PNG.
PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
    "nGNgYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg=="
)


def _settings(tmp_path: str) -> Settings:
    return Settings(
        image_base_url="http://image.test/v1",
        image_api_key="test-image-key",
        image_model="gpt-image-2",
        image_quality="medium",
        image_max_count=3,
        data_dir=tmp_path,
        _env_file=None,
    )


def _b64_response(data: bytes = PNG_1x1) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "gen-123",
            "created": 1,
            "data": [{"b64_json": base64.b64encode(data).decode()}],
        },
    )


def _make(handler, tmp_path):
    return OpenAIImageProvider(
        settings=_settings(tmp_path),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://image.test/v1",
        ),
        backoff_seconds=FAST_BACKOFF,
    )


async def _gen(provider: OpenAIImageProvider, job_id: str = "job-1") -> None:
    await provider.generate(
        ImageGenerationRequest(
            prompt="a calm window scene",
            filename="hero.webp",
            alt_text="hero alt",
            aspect_ratio="16:9",
            job_id=job_id,
        )
    )


async def test_generate_stores_file_section34(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.headers["authorization"]))
        import json

        body = json.loads(request.content)
        assert body["model"] == "gpt-image-2"
        assert body["size"] == "1536x1024"  # 16:9
        return _b64_response()

    provider = _make(handler, str(tmp_path))
    try:
        image = await provider.generate(
            ImageGenerationRequest(
                prompt="p",
                filename="hero.webp",
                job_id="job-1",
            )
        )
    finally:
        await provider.aclose()

    # data/articles/{job}/images/hero.webp (section 34)
    assert image.local_path.endswith("articles/job-1/images/hero.webp")
    assert (tmp_path / "articles/job-1/images/hero.webp").read_bytes() == PNG_1x1
    assert image.mime_type == "image/png"
    assert image.provider == "gpt-image-2"
    assert image.provider_request_id == "gen-123"
    # Auth header sent exactly once, key present (never logged — tested
    # separately by the request shape; we only assert it is sent).
    assert calls[0][1] == "Bearer test-image-key"


async def test_generate_url_payload(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/images/generations":
            return httpx.Response(
                200,
                json={
                    "id": "gen-u",
                    "data": [{"url": "http://image.test/v1/img.png"}],
                },
            )
        seen.append(request.url.path)
        return httpx.Response(200, content=PNG_1x1)

    provider = _make(handler, str(tmp_path))
    try:
        image = await provider.generate(
            ImageGenerationRequest(
                prompt="p", filename="inline-1.webp", job_id="job-2"
            )
        )
    finally:
        await provider.aclose()
    assert seen == ["/v1/img.png"]
    assert (tmp_path / "articles/job-2/images/inline-1.webp").exists()


async def test_generate_retries_on_429_then_succeeds(tmp_path):
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] < 3:
            return httpx.Response(429, json={"error": {"message": "slow"}})
        return _b64_response()

    provider = _make(handler, str(tmp_path))
    try:
        await _gen(provider)
    finally:
        await provider.aclose()
    assert state["n"] == 3  # 1 + 2 retries (spec section 51: Image 2 retry)


async def test_generate_exhausts_retries(tmp_path):
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(503)

    provider = _make(handler, str(tmp_path))
    try:
        with pytest.raises(PipelineError) as excinfo:
            await _gen(provider)
    finally:
        await provider.aclose()
    assert excinfo.value.error_code == ErrorCode.IMAGE_PROVIDER_FAILED
    assert state["n"] == 3  # max 3 attempts


async def test_generate_non_retryable_400(tmp_path):
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(400, json={"error": "bad prompt"})

    provider = _make(handler, str(tmp_path))
    try:
        with pytest.raises(PipelineError) as excinfo:
            await _gen(provider)
    finally:
        await provider.aclose()
    assert excinfo.value.error_code == ErrorCode.IMAGE_PROVIDER_FAILED
    assert state["n"] == 1  # no retry for 4xx


async def test_generate_empty_filename_fails(tmp_path):
    provider = _make(lambda r: _b64_response(), str(tmp_path))
    try:
        with pytest.raises(PipelineError) as excinfo:
            await provider.generate(
                ImageGenerationRequest(prompt="p", filename="")
            )
    finally:
        await provider.aclose()
    assert excinfo.value.error_code == ErrorCode.IMAGE_PROVIDER_FAILED


async def test_generate_garbage_payload_fails(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": "x", "data": [{"b64_json": "!!!not-base64!!!"}]},
        )

    provider = _make(handler, str(tmp_path))
    try:
        with pytest.raises(PipelineError) as excinfo:
            await _gen(provider)
    finally:
        await provider.aclose()
    assert excinfo.value.error_code == ErrorCode.IMAGE_PROVIDER_FAILED


async def test_health_check_true_when_model_served(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200, json={"data": [{"id": "gpt-image-2"}, {"id": "text"}]}
        )

    provider = _make(handler, str(tmp_path))
    try:
        assert await provider.health_check() is True
    finally:
        await provider.aclose()


async def test_health_check_false_when_model_missing(tmp_path):
    provider = _make(
        lambda r: httpx.Response(200, json={"data": [{"id": "text"}]}),
        str(tmp_path),
    )
    try:
        assert await provider.health_check() is False
    finally:
        await provider.aclose()


async def test_health_check_false_when_endpoint_down(tmp_path):
    provider = _make(
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")),
        str(tmp_path),
    )
    try:
        assert await provider.health_check() is False
    finally:
        await provider.aclose()
