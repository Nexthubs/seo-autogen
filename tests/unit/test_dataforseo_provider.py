"""P2 DataForSEO provider tests via httpx.MockTransport
(spec sections 10.2, 12, 51, 52; audit H01, M12, M15).

Fixtures are pinned to the REAL, desensitized official DataForSEO envelope
(machine-verified against the documented Google Organic Advanced response):

  - top-level business ``status_code == 20000`` (HTTP is always 200)
  - per-task ``status_code`` / ``cost`` (USD, NOT ``credits``)
  - ``tasks[0].result`` is a LIST of SERP blocks; each block's ``items[]``
    is a FLAT, MIXED-type list dispatched by the item's own ``type``
  - organic rank comes from ``rank_group`` (``position`` is a left/right
    string, not a rank)
  - PAA: item ``people_also_ask`` -> ``items[]`` of
    ``people_also_ask_element`` (question = element ``title``, source url =
    first ``expanded_element`` entry's ``url``, which may be absent)
  - related: item ``related_searches`` -> ``items`` is a list of strings
  - featured: item ``featured_snippet`` with top-level title/url/description

Covers:
  - task body per spec section 12 (array, fields, OS, PAA click depth flag)
  - Standard task_post -> task_get polling flow
  - HTTP Basic Auth header present
  - parse(): organic / PAA / related / featured snippet, mixed item types
  - rank ordering by ``rank_group`` from a shuffled provider order
  - task failure (result null / task status_code != 20000)
    -> DATAFORSEO_REQUEST_FAILED
  - empty organic -> DATAFORSEO_EMPTY_SERP
  - raw retention: full payload kept, ``status_code == 20000``
  - cost: read from ``cost`` (task-level, top-level fallback), never
    ``credits`` (audit M12)
  - retry policy: 429 retried then success; 401 -> DATAFORSEO_AUTH_FAILED
    with no retry; 500 x3 -> DATAFORSEO_REQUEST_FAILED
  - health_check (audit M15): unconfigured -> False, HTTP 401 -> False,
    HTTP 200 + business failure -> False, official success -> True
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
        dataforseo_request_type="live",
        dataforseo_location_code=2840,
        dataforseo_language_code="en",
        dataforseo_device="desktop",
        dataforseo_os="windows",
        dataforseo_depth=10,
        dataforseo_paa_click_depth=0,
        dataforseo_load_async_ai_overview=False,
        dataforseo_calculate_rectangles=False,
        dataforseo_poll_interval_seconds=0,
        dataforseo_poll_timeout_seconds=1,
    )
    base.update(over)
    return Settings(**base, _env_file=None)


def _organic_item(rank_group: int, idx: int) -> dict:
    return {
        "type": "organic",
        "rank_group": rank_group,
        "rank_absolute": rank_group + 4,
        "page": 1,
        "position": "left",
        "domain": f"site{idx}.example.com",
        "title": f"Title {rank_group}",
        "url": f"https://site{idx}.example.com/article-{rank_group}",
        "description": f"Snippet {rank_group}",
    }


def _paa_item() -> dict:
    return {
        "type": "people_also_ask",
        "rank_group": 1,
        "rank_absolute": 3,
        "items": [
            {
                "type": "people_also_ask_element",
                "title": "Why does no contact work?",
                "seed_question": None,
                "xpath": "/html/body[1]/div[3]",
                "expanded_element": [
                    {
                        "type": "people_also_ask_expanded_element",
                        "featured_title": None,
                        "url": "https://paa.example.com/1",
                        "domain": "paa.example.com",
                        "title": "No Contact Explained",
                        "description": "snippet",
                    }
                ],
            },
            # Second element: expanded by the AI overview variant, which has
            # no ``url`` key — the parser must keep the question with a
            # None source_url.
            {
                "type": "people_also_ask_element",
                "title": "How long should you do no contact?",
                "seed_question": None,
                "expanded_element": [
                    {
                        "type": "people_also_ask_ai_overview_expanded_element",
                        "items": [
                            {"type": "ai_overview_element", "title": "AI note"}
                        ],
                    }
                ],
            },
        ],
    }


def _related_item() -> dict:
    return {
        "type": "related_searches",
        "rank_group": 1,
        "rank_absolute": 8,
        "items": ["no contact rules", "anxious attachment style"],
    }


def _featured_item() -> dict:
    return {
        "type": "featured_snippet",
        "rank_group": 1,
        "rank_absolute": 10,
        "domain": "featured.example.com",
        "title": "Featured snippet title",
        "featured_title": None,
        "description": "Featured snippet description",
        "url": "https://featured.example.com/answer",
    }


def _noise_items() -> list[dict]:
    """Non-organic SERP features, in the official mixed order (a carousel
    item may even lead the list)."""
    return [
        {
            "type": "carousel",
            "rank_group": 1,
            "rank_absolute": 3,
            "items": [{"type": "carousel_element", "title": "Carousel"}],
        },
        {
            "type": "paid",
            "rank_group": 1,
            "rank_absolute": 1,
            "url": "https://paid.example.com",
        },
        {"type": "ai_overview", "rank_group": 1, "rank_absolute": 1},
    ]


def _serp_body(
    *,
    organic_count: int = 6,
    organic_ranks: list[int] | None = None,
    with_paa: bool = True,
    with_related: bool = True,
    with_featured: bool = False,
    with_noise: bool = True,
    cost: float | None = 0.03,
    top_cost: float | None = None,
) -> dict:
    """A desensitized official envelope with ``organic_count`` organic items.

    ``organic_ranks`` overrides the per-item ``rank_group`` values (use a
    shuffled order to prove the parser sorts by ``rank_group``).
    """
    ranks = organic_ranks or list(range(1, organic_count + 1))
    items = _noise_items() if with_noise else []
    items.extend(
        _organic_item(rank, idx) for idx, rank in enumerate(ranks, start=1)
    )
    if with_paa:
        items.append(_paa_item())
    if with_related:
        items.append(_related_item())
    if with_featured:
        items.append(_featured_item())

    task = {
        "status_code": 20000,
        "status_message": "OK",
        "id": "task-1",
        "path": "/v3/serp/google/organic/live/advanced",
        "result_count": 1,
        "time": "0.2",
        "data": {},
        "result": [
            {
                "type": "organic",
                "keyword": "anxious attachment no contact",
                "item_types": sorted({i["type"] for i in items}),
                "items_count": len(items),
                "items": items,
            }
        ],
    }
    if cost is not None:
        task["cost"] = cost
    # Deliberate red herring: the old parser read this field. The official
    # cost field is ``cost`` (USD).
    task["credits"] = 999

    body: dict = {
        "version": "2026-09-01",
        "status_code": 20000,
        "status_message": "OK",
        "time": "0.3",
        "tasks_count": 1,
        "tasks_error": 0,
        "tasks": [task],
    }
    if top_cost is not None:
        body["cost"] = top_cost
    return body


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
        "os": "windows",
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


async def test_standard_posts_task_then_gets_advanced_result():
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/task_post"):
            body = {
                "status_code": 20000,
                "tasks": [
                    {
                        "id": "standard-task-1",
                        "status_code": 20100,
                        "status_message": "Task Created.",
                        "cost": 0.0006,
                    }
                ],
            }
            return httpx.Response(200, json=body)
        return httpx.Response(200, json=_serp_body(cost=0.0006))

    p = _provider(
        handler,
        dataforseo_request_type="standard",
        dataforseo_os="ios",
        dataforseo_device="mobile",
    )
    response = await p.search(
        SERPRequest(
            keyword="standard request",
            location_code=2840,
            language_code="en",
            device="mobile",
            depth=10,
        )
    )

    assert calls == [
        ("POST", "/v3/serp/google/organic/task_post"),
        ("GET", "/v3/serp/google/organic/task_get/advanced/standard-task-1"),
    ]
    assert response.provider_cost == pytest.approx(0.0006)


async def test_standard_polls_pending_task_until_ready():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.url.path.endswith("/task_post"):
            return httpx.Response(
                200,
                json={
                    "status_code": 20000,
                    "tasks": [
                        {
                            "id": "standard-task-2",
                            "status_code": 20100,
                            "cost": 0.0006,
                        }
                    ],
                },
            )
        if calls["n"] == 2:
            return httpx.Response(
                200,
                json={
                    "status_code": 20000,
                    "tasks": [
                        {
                            "id": "standard-task-2",
                            "status_code": 40602,
                            "status_message": "Task In Queue.",
                            "result": None,
                        }
                    ],
                },
            )
        return httpx.Response(200, json=_serp_body(cost=0.0006))

    p = _provider(
        handler,
        dataforseo_request_type="standard",
        dataforseo_poll_interval_seconds=0,
    )
    response = await p.search(
        SERPRequest(keyword="k", location_code=2840, language_code="en")
    )

    assert calls["n"] == 3
    assert response.organic_results


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
    assert resp.paa_questions[0].question == "Why does no contact work?"
    assert resp.paa_questions[0].source_url == "https://paa.example.com/1"
    assert resp.related_searches == ["no contact rules", "anxious attachment style"]
    # full raw payload kept for audit (spec 12.2)
    assert resp.raw["status_code"] == 20000


async def test_search_sorts_organic_by_rank_group_not_input_order():
    # Provider item order is NOT rank order (official example: a carousel
    # leads, organic sits at rank_group 26). Shuffled input must come out
    # sorted by rank_group.
    p = _provider(
        lambda r: httpx.Response(
            200, json=_serp_body(organic_ranks=[5, 1, 3, 2, 6, 4])
        )
    )
    resp = await p.search(
        SERPRequest(keyword="k", location_code=2840, language_code="en")
    )
    assert [r.rank for r in resp.organic_results] == [1, 2, 3, 4, 5, 6]
    # rank 2 item is the one at index 1 in the shuffled input
    assert resp.organic_results[0].url == "https://site2.example.com/article-1"


async def test_search_mixed_item_types_only_organic_collected():
    # paid/video/carousel/ai_overview never reach organic (spec 12.1) even
    # though they live in the SAME flat items[] list as organic results.
    body = _serp_body(with_noise=True, organic_count=2, with_paa=False,
                      with_related=False)
    p = _provider(lambda r: httpx.Response(200, json=body))
    resp = await p.search(
        SERPRequest(keyword="k", location_code=2840, language_code="en")
    )
    assert len(resp.organic_results) == 2
    assert all("paid" not in r.url for r in resp.organic_results)
    assert all("carousel" not in r.url for r in resp.organic_results)


async def test_search_featured_snippet_parsed():
    p = _provider(
        lambda r: httpx.Response(200, json=_serp_body(with_featured=True))
    )
    resp = await p.search(
        SERPRequest(keyword="k", location_code=2840, language_code="en")
    )
    assert resp.featured_snippet is not None
    assert resp.featured_snippet.title == "Featured snippet title"
    assert resp.featured_snippet.url == "https://featured.example.com/answer"
    assert resp.featured_snippet.snippet == "Featured snippet description"
    assert resp.featured_snippet.domain == "featured.example.com"


async def test_search_no_featured_when_absent():
    p = _provider(lambda r: httpx.Response(200, json=_serp_body()))
    resp = await p.search(
        SERPRequest(keyword="k", location_code=2840, language_code="en")
    )
    assert resp.featured_snippet is None


async def test_search_paa_expanded_without_url_yields_none_source():
    # The AI-overview expanded element variant has no ``url`` key — the
    # question must survive with a None source_url (defensive parse).
    p = _provider(lambda r: httpx.Response(200, json=_serp_body()))
    resp = await p.search(
        SERPRequest(keyword="k", location_code=2840, language_code="en")
    )
    second = resp.paa_questions[1]
    assert second.question == "How long should you do no contact?"
    assert second.source_url is None


async def test_search_empty_organic_raises():
    p = _provider(
        lambda r: httpx.Response(
            200, json=_serp_body(organic_count=0, with_paa=False,
                                 with_related=False)
        )
    )
    with pytest.raises(PipelineError) as exc:
        await p.search(SERPRequest(keyword="k", location_code=2840, language_code="en"))
    assert exc.value.error_code == ErrorCode.DATAFORSEO_EMPTY_SERP


async def test_task_failure_result_null_raises_request_failed():
    # Task-level failure in the official envelope: task status_code !=
    # 20000 and result null (a task error, not an empty SERP).
    body = _serp_body(organic_count=1)
    body["tasks"][0]["status_code"] = 40101
    body["tasks"][0]["status_message"] = "invalid credentials"
    body["tasks"][0]["result"] = None
    p = _provider(lambda r: httpx.Response(200, json=body))
    with pytest.raises(PipelineError) as exc:
        await p.search(SERPRequest(keyword="k", location_code=2840, language_code="en"))
    assert exc.value.error_code == ErrorCode.DATAFORSEO_REQUEST_FAILED


async def test_http_200_business_failure_raises_request_failed():
    # DataForSEO always returns HTTP 200; the business outcome is the
    # top-level status_code. 40101 = invalid credentials (audit M15).
    body = _serp_body(organic_count=1)
    body["status_code"] = 40101
    body["status_message"] = "invalid credentials"
    p = _provider(lambda r: httpx.Response(200, json=body))
    with pytest.raises(PipelineError) as exc:
        await p.search(SERPRequest(keyword="k", location_code=2840, language_code="en"))
    assert exc.value.error_code == ErrorCode.DATAFORSEO_REQUEST_FAILED


async def test_http_status_not_200_raises_request_failed():
    p = _provider(
        lambda r: httpx.Response(200, json={"status_code": 400, "message": "bad"})
    )
    with pytest.raises(PipelineError) as exc:
        await p.search(SERPRequest(keyword="k", location_code=2840, language_code="en"))
    assert exc.value.error_code == ErrorCode.DATAFORSEO_REQUEST_FAILED


# ------------------------------------------------------------
# cost (audit M12: official field is ``cost`` USD, never ``credits``)
# ------------------------------------------------------------
async def test_cost_read_from_cost_field_not_credits():
    # The fixture carries credits=999 on purpose; the parser must ignore it.
    p = _provider(
        lambda r: httpx.Response(200, json=_serp_body(cost=0.12, top_cost=None))
    )
    resp = await p.search(
        SERPRequest(keyword="k", location_code=2840, language_code="en")
    )
    assert resp.provider_cost == pytest.approx(0.12)


async def test_cost_falls_back_to_top_level_cost():
    body = _serp_body(cost=None, top_cost=0.05)
    p = _provider(lambda r: httpx.Response(200, json=body))
    resp = await p.search(
        SERPRequest(keyword="k", location_code=2840, language_code="en")
    )
    assert resp.provider_cost == pytest.approx(0.05)


async def test_cost_none_when_absent():
    body = _serp_body(cost=None, top_cost=None)
    p = _provider(lambda r: httpx.Response(200, json=body))
    resp = await p.search(
        SERPRequest(keyword="k", location_code=2840, language_code="en")
    )
    assert resp.provider_cost is None


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
# health_check (audit M15)
# ------------------------------------------------------------
async def test_health_check_unconfigured_false():
    p = DataForSEOSERPProvider(
        settings=Settings(
            dataforseo_login="", dataforseo_password="", _env_file=None
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
    )
    assert await p.health_check() is False


async def test_health_check_http_401_false():
    p = _provider(lambda r: httpx.Response(401, text="bad"))
    assert await p.health_check() is False


async def test_health_check_http_200_business_failure_false():
    # HTTP 200 + business auth failure must NOT read as Connected.
    body = _serp_body(organic_count=1)
    body["status_code"] = 40101
    body["status_message"] = "invalid credentials"
    p = _provider(lambda r: httpx.Response(200, json=body))
    assert await p.health_check() is False


async def test_health_check_ok_true():
    p = _provider(lambda r: httpx.Response(200, json=_serp_body(organic_count=1)))
    assert await p.health_check() is True
