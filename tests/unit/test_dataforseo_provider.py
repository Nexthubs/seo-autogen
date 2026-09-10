"""P2 DataForSEO provider tests via httpx.MockTransport
(spec sections 10.2, 12, 51, 52).

Covers:
  - task body per spec section 12 (array, fields, PAA click depth flag)
  - HTTP Basic Auth header present
  - parse(): organic/PAA/related/featured snippet
  - search(): happy path, empty organic -> DATAFORSEO_EMPTY_SERP
  - task-level error result -> DATAFORSEO_EMPTY_SERP
  - retry policy: 429 retried then success; 401 -> DATAFORSEO_AUTH_FAILED
    with no retry; 500 x3 -> DATAFORSEO_REQUEST_FAILED
  - health_check: unconfigured -> False, auth failure -> False, ok -> True
"""

import base64
import json

import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import ErrorCode, PipelineError
from app.providers.serp.dataforseo import DataForSEOSERPProvider
from app.schemas.serp import SERPRequest

FAST_BACKOFF = (0.001, 0.001, 0.001)


def _settings(**over) -> Settings:
    base = dict(
        dataforseo_base_url="https://api.dataforseo.test",
        dataforseo_login="user",
        dataforseo_password="pass",
        dataforseo_location_code=2840,
        dataforseo_language_code="en",
        dataforseo_device="desktop",
        dataforseo_depth=10,
        dataforseo_paa_click_depth=0,
        dataforseo_load_async_ai_overview=False,
        dataforseo_calculate_rectangles=False,
    )
    base.update(over)
    return Settings(**base, _env_file=None)


def _serp_body(
    *,
    organic_count: int = 6,
    with_paa: bool = True,
    with_related: bool = True,
    with_featured: bool = False,
) -> dict:
    organic = [
        {
            "position": i,
            "title": f"Title {i}",
            "url": f"https://site{i}.example.com/article-{i}",
            "domain": f"site{i}.example.com",
            "description": f"Snippet {i}",
        }
        for i in range(1, organic_count + 1)
    ]
    result = {"organic": organic}
    if with_paa:
        result["people_also_ask"] = [
            {
                "questions": [
                    {"question": "Why no contact works?", "url": "https://paa.example.com/1"},
                    {"question": "How long is no contact?", "url": None},
                ]
            }
        ]
    if with_related:
        result["related_searches"] = [
            {"title": "no contact rules"},
            {"title": "anxious attachment style"},
        ]
    if with_featured:
        result["featured_snippet"] = {
            "position": 0,
            "url": "https://featured.example.com",
            "title": "Featured",
        }
    return {
        "status_code": 200,
        "status_message": "OK",
        "tasks": [
            {
                "cost": 0.03,
                "credits": 1,
                "result": result,
            }
        ],
    }


def _provider(handler, **settings_over) -> DataForSEOSERPProvider:
    settings = _settings(**settings_over)
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        base_url=settings.dataforseo_base_url,
        transport=transport,
        auth=(settings.dataforseo_login, settings.dataforseo_password),
    )
    return DataForSEOSERPProvider(
        settings=_settings(**settings_over),
        client=client,
        backoff_seconds=FAST_BACKOFF,
    )


# ------------------------------------------------------------
# task building (spec section 12)
# ------------------------------------------------------------
def test_build_task_default_fields():
    p = _provider(lambda r: httpx.Response(200, json={}))
    req = SERPRequest(
        keyword="anxious attachment no contact",
        location_code=2840,
        language_code="en",
        device="desktop",
        depth=10,
    )
    task = p.build_task(req)
    assert task == {
        "keyword": "anxious attachment no contact",
        "location_code": 2840,
        "language_code": "en",
        "device": "desktop",
        "depth": 10,
        "calculate_rectangles": False,
        "load_async_ai_overview": False,
    }
    # PAA click depth flag only when > 0 (spec section 12)
    assert "people_also_ask_click_depth" not in task


def test_build_task_includes_paa_click_depth_when_positive():
    p = _provider(
        lambda r: httpx.Response(200, json={}),
        dataforseo_paa_click_depth=1,
    )
    task = p.build_task(
        SERPRequest(
            keyword="k", location_code=2840, language_code="en"
        )
    )
    assert task["people_also_ask_click_depth"] == 1


async def test_request_sends_basic_auth_and_array_body():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization", "")
        captured["body"] = json.loads(request.content)
        captured["path"] = request.url.path
        return httpx.Response(200, json=_serp_body())

    p = _provider(handler)
    await p.search(
        SERPRequest(
            keyword="anxious attachment no contact",
            location_code=2840,
            language_code="en",
            device="desktop",
            depth=10,
        )
    )
    expected = base64.b64encode(b"user:pass").decode()
    assert captured["auth"] == f"Basic {expected}"
    assert captured["path"] == "/v3/serp/google/organic/live/advanced"
    assert isinstance(captured["body"], list) and len(captured["body"]) == 1


# ------------------------------------------------------------
# parsing
# ------------------------------------------------------------
async def test_search_happy_path():
    p = _provider(lambda r: httpx.Response(200, json=_serp_body()))
    resp = await p.search(
        SERPRequest(
            keyword="k", location_code=2840, language_code="en"
        )
    )
    assert resp.keyword == "k"
    assert len(resp.organic_results) == 6
    assert [r.rank for r in resp.organic_results] == [1, 2, 3, 4, 5, 6]
    assert resp.organic_results[0].domain == "site1.example.com"
    assert resp.paa_questions[0].question == "Why no contact works?"
    assert resp.paa_questions[0].source_url == "https://paa.example.com/1"
    assert resp.related_searches == ["no contact rules", "anxious attachment style"]
    # full raw payload kept for audit (spec 12.2)
    assert resp.raw["status_code"] == 200


async def test_search_ignores_non_organic_features():
    # paid/video/shopping never reach organic (spec 12.1); only `organic`
    # list is parsed, so nothing to filter — verify parser only reads it.
    body = _serp_body()
    body["tasks"][0]["result"]["paid"] = [
        {"position": 1, "url": "https://paid.example.com"}
    ]
    p = _provider(lambda r: httpx.Response(200, json=body))
    resp = await p.search(
        SERPRequest(
            keyword="k", location_code=2840, language_code="en"
        )
    )
    assert all("paid" not in r.url for r in resp.organic_results)


async def test_search_empty_organic_raises():
    p = _provider(lambda r: httpx.Response(200, json=_serp_body(organic_count=0)))
    with pytest.raises(PipelineError) as exc:
        await p.search(SERPRequest(keyword="k", location_code=2840, language_code="en"))
    assert exc.value.error_code == ErrorCode.DATAFORSEO_EMPTY_SERP


async def test_task_level_error_result_raises_empty_serp():
    body = {
        "status_code": 200,
        "tasks": [
            {
                "cost": 0,
                "credits": 0,
                "result": {"code": 401, "message": "invalid credentials"},
            }
        ],
    }
    p = _provider(lambda r: httpx.Response(200, json=body))
    with pytest.raises(PipelineError) as exc:
        await p.search(SERPRequest(keyword="k", location_code=2840, language_code="en"))
    assert exc.value.error_code == ErrorCode.DATAFORSEO_EMPTY_SERP


async def test_http_status_not_200_raises_request_failed():
    p = _provider(
        lambda r: httpx.Response(200, json={"status_code": 400, "message": "bad"})
    )
    with pytest.raises(PipelineError) as exc:
        await p.search(SERPRequest(keyword="k", location_code=2840, language_code="en"))
    assert exc.value.error_code == ErrorCode.DATAFORSEO_REQUEST_FAILED


# ------------------------------------------------------------
# retry policy (spec section 51)
# ------------------------------------------------------------
async def test_429_retried_then_success():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"message": "rate limited"})
        return httpx.Response(200, json=_serp_body(organic_count=1))

    p = _provider(handler)
    resp = await p.search(
        SERPRequest(keyword="k", location_code=2840, language_code="en")
    )
    assert calls["n"] == 2
    assert len(resp.organic_results) == 1


async def test_401_not_retried_auth_failed():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="bad login")

    p = _provider(handler)
    with pytest.raises(PipelineError) as exc:
        await p.search(SERPRequest(keyword="k", location_code=2840, language_code="en"))
    assert exc.value.error_code == ErrorCode.DATAFORSEO_AUTH_FAILED
    assert calls["n"] == 1


async def test_500_exhaustion_raises_request_failed():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    p = _provider(handler)
    with pytest.raises(PipelineError) as exc:
        await p.search(SERPRequest(keyword="k", location_code=2840, language_code="en"))
    assert exc.value.error_code == ErrorCode.DATAFORSEO_REQUEST_FAILED
    assert calls["n"] == 3


# ------------------------------------------------------------
# health_check
# ------------------------------------------------------------
async def test_health_check_unconfigured_false():
    p = DataForSEOSERPProvider(
        settings=Settings(
            dataforseo_login="", dataforseo_password="", _env_file=None
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
    )
    assert await p.health_check() is False


async def test_health_check_auth_failure_false():
    p = _provider(lambda r: httpx.Response(401, text="bad"))
    assert await p.health_check() is False


async def test_health_check_ok_true():
    p = _provider(lambda r: httpx.Response(200, json=_serp_body(organic_count=1)))
    assert await p.health_check() is True
