"""P2 Exa extractor tests via httpx.MockTransport
(spec sections 10.3, 15, 51, 52).

Covers:
  - /contents request with x-api-key header and {"ids": [...]} body
  - one ExtractedPage per successful result
  - deterministic truncation at SOURCE_MAX_CHARS (spec 15)
  - per-page failure/empty skipped; all failed -> SOURCE_EMPTY
  - 401 -> EXTRACTOR_AUTH_FAILED (no retry), 429 retried
  - health_check
"""

import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import ErrorCode, PipelineError
from app.providers.extractor.exa import ExaContentExtractor, truncate_content

FAST_BACKOFF = (0.001, 0.001, 0.001)


def _settings(**over) -> Settings:
    base = dict(
        exa_base_url="https://api.exa.test",
        exa_api_key="test-key",
        source_max_chars=50000,
    )
    base.update(over)
    return Settings(**base, _env_file=None)


def _provider(handler, **settings_over) -> ExaContentExtractor:
    settings = _settings(**settings_over)
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        base_url=settings.exa_base_url,
        transport=transport,
        headers={"x-api-key": settings.exa_api_key},
    )
    return ExaContentExtractor(
        settings=_settings(**settings_over),
        client=client,
        backoff_seconds=FAST_BACKOFF,
    )


def _contents_body(items, statuses=None) -> dict:
    body = {"results": items}
    if statuses is not None:
        body["statuses"] = statuses
    return body


async def test_extract_sends_api_key_and_ids():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["key"] = request.headers.get("x-api-key", "")
        captured["body"] = __import__("json").loads(request.content)
        captured["path"] = request.url.path
        return httpx.Response(
            200,
            json=_contents_body(
                [
                    {
                        "id": "https://a.example.com/1",
                        "title": "Page A",
                        "url": "https://a.example.com/1",
                        "text": "Content A",
                    }
                ]
            ),
        )

    p = _provider(handler)
    pages = await p.extract(["https://a.example.com/1"])
    assert captured["key"] == "test-key"
    assert captured["path"] == "/contents"
    assert captured["body"] == {"ids": ["https://a.example.com/1"]}
    assert len(pages) == 1
    assert pages[0].title == "Page A"
    assert pages[0].content_markdown == "Content A"
    assert pages[0].normalized_url == "https://a.example.com/1"
    assert pages[0].extractor == "exa"


async def test_extract_truncates_long_content_deterministically():
    long_text = "x" * 60000
    p = _provider(
        lambda r: httpx.Response(
            200,
            json=_contents_body(
                [
                    {
                        "id": "https://a.example.com/1",
                        "url": "https://a.example.com/1",
                        "title": "Long",
                        "text": long_text,
                    }
                ]
            ),
        ),
        source_max_chars=50000,
    )
    pages = await p.extract(["https://a.example.com/1"])
    assert len(pages[0].content_markdown) == 50000
    # deterministic: same input + budget -> same output
    assert truncate_content(long_text, 50000) == long_text[:50000]
    assert truncate_content(long_text, 50000) == pages[0].content_markdown


async def test_extract_skips_failed_and_empty_pages():
    body = _contents_body(
        items=[
            {
                "id": "https://ok.example.com/1",
                "url": "https://ok.example.com/1",
                "title": "OK",
                "text": "fine",
            },
            {"id": "https://dead.example.com/1", "url": "https://dead.example.com/1"},
            {
                "id": "https://empty.example.com/1",
                "url": "https://empty.example.com/1",
                "title": "Empty",
                "text": "   ",
            },
        ],
        statuses=[
            {"id": "https://ok.example.com/1", "status": "success"},
            {"id": "https://dead.example.com/1", "status": "error"},
            {"id": "https://empty.example.com/1", "status": "success"},
        ],
    )
    p = _provider(lambda r: httpx.Response(200, json=body))
    pages = await p.extract(
        [
            "https://ok.example.com/1",
            "https://dead.example.com/1",
            "https://empty.example.com/1",
        ]
    )
    assert [p_.url for p_ in pages] == ["https://ok.example.com/1"]


async def test_extract_all_failed_raises_source_empty():
    body = _contents_body(
        items=[
            {"id": "https://dead.example.com/1", "url": "https://dead.example.com/1"}
        ],
        statuses=[
            {"id": "https://dead.example.com/1", "status": "error"}
        ],
    )
    p = _provider(lambda r: httpx.Response(200, json=body))
    with pytest.raises(PipelineError) as exc:
        await p.extract(["https://dead.example.com/1"])
    assert exc.value.error_code == ErrorCode.SOURCE_EMPTY


async def test_extract_no_urls_raises_source_empty():
    p = _provider(lambda r: httpx.Response(200, json={}))
    with pytest.raises(PipelineError) as exc:
        await p.extract([])
    assert exc.value.error_code == ErrorCode.SOURCE_EMPTY


async def test_401_not_retried_auth_failed():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"message": "invalid x-api-key"})

    p = _provider(handler)
    with pytest.raises(PipelineError) as exc:
        await p.extract(["https://a.example.com/1"])
    assert exc.value.error_code == ErrorCode.EXTRACTOR_AUTH_FAILED
    assert calls["n"] == 1


async def test_429_retried_then_success():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"message": "slow down"})
        return httpx.Response(
            200,
            json=_contents_body(
                [
                    {
                        "id": "https://a.example.com/1",
                        "url": "https://a.example.com/1",
                        "title": "A",
                        "text": "ok",
                    }
                ]
            ),
        )

    p = _provider(handler)
    pages = await p.extract(["https://a.example.com/1"])
    assert calls["n"] == 2
    assert len(pages) == 1


# ------------------------------------------------------------
# health_check
# ------------------------------------------------------------
async def test_health_check_unconfigured_false():
    p = ExaContentExtractor(
        settings=Settings(exa_api_key="", _env_file=None),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        ),
    )
    assert await p.health_check() is False


async def test_health_check_reachable_true():
    p = _provider(lambda r: httpx.Response(200, json={"status": "ok"}))
    assert await p.health_check() is True
