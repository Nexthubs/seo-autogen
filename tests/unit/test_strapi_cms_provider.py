"""P7 unit: StrapiCMSProvider (spec sections 35-42, 51, 60).

All traffic goes through ``httpx.MockTransport`` — no network. The
fake server records requests and serves scripted responses.
"""

import json

import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import ErrorCode, PipelineError
from app.providers.cms.strapi_cms import StrapiCMSProvider
from app.schemas.strapi import MediaUploadResult

FAST_BACKOFF = (0.001, 0.001, 0.001)
TOKEN = "super-secret-token-123"


def q(request: httpx.Request) -> str:
    """The query string as text (url.query is bytes under MockTransport)."""
    return request.url.query.decode("utf-8")


def _settings() -> Settings:
    return Settings(
        llm_base_url="http://llm.test/v1",
        llm_api_key="test-key",
        llm_model="test-model",
        strapi_base_url="http://strapi.test",
        strapi_api_token=TOKEN,
        strapi_blog_plural_api_id="blogs",
        strapi_blog_uid="api::blog.blog",
        strapi_default_author_document_id="doc-author-1",
        strapi_default_category_document_id="doc-cat-1",
        _env_file=None,
    )


class FakeStrapi:
    """Records every request; serves scripted JSON by (method, path)."""

    def __init__(self, routes: dict | None = None):
        self.requests: list[httpx.Request] = []
        self.routes = routes or {}
        self._queue: list[httpx.Response] = []

    def enqueue(self, response: httpx.Response) -> None:
        self._queue.append(response)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._queue:
            return self._queue.pop(0)
        key = (request.method, request.url.path)
        if key in self.routes:
            return self.routes[key]
        return httpx.Response(200, json={"data": []})

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if r.method == method and r.url.path == path
        ]


@pytest.fixture()
def fake():
    return FakeStrapi()


@pytest.fixture()
def provider(fake):
    client = httpx.AsyncClient(
        base_url="http://strapi.test", transport=httpx.MockTransport(fake)
    )
    return StrapiCMSProvider(
        settings=_settings(), client=client, backoff_seconds=FAST_BACKOFF
    )


def _blog_item(item_id=7, doc_id="doc-abc", **fields):
    """A Strapi 5 (V1 target) Blog item — FLAT (H03).

    Strapi 5 returns ``{"id", "documentId", "title", ...}`` directly:
    there is NO ``attributes`` wrapper (that was Strapi 4).
    """
    data = {
        "id": item_id,
        "documentId": doc_id,
        "title": "T",
        "slug": "slug",
        "status": "draft",
        "body": "b",
        "metaTitle": "mt",
        "metaDescription": "md",
        "seoKeywords": "kw",
        "author": "doc-author-1",
        "category": "doc-cat-1",
        "mainImage": "https://cms/uploads/hero.webp",
    }
    data.update(fields)
    return data


def _blog_item_v4(item_id=7, doc_id="doc-abc", **attributes):
    """A legacy Strapi 4 Blog item — wrapped in ``attributes``."""
    attrs = {
        "title": "T",
        "slug": "slug",
        "status": "draft",
        "body": "b",
        "metaTitle": "mt",
        "metaDescription": "md",
        "seoKeywords": "kw",
        "author": "doc-author-1",
        "category": "doc-cat-1",
        "mainImage": "https://cms/uploads/hero.webp",
    }
    attrs.update(attributes)
    return {"id": item_id, "documentId": doc_id, "attributes": attrs}


# ------------------------------------------------------------------ discovery
async def test_health_check_uses_schema_discovery_endpoint(fake, provider):
    assert await provider.health_check() is True
    req = fake.calls("GET", "/api/blogs")[0]
    assert "pagination%5BpageSize%5D=1" in q(req)
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"


async def test_health_check_false_on_auth_error(fake, provider):
    fake.enqueue(httpx.Response(401, json={"error": {"message": "bad"}}))
    assert await provider.health_check() is False


async def test_list_authors_and_categories(fake, provider):
    fake.routes[("GET", "/api/authors")] = httpx.Response(
        200, json={"data": [{"id": 1, "documentId": "a1"}]}
    )
    fake.routes[("GET", "/api/categories")] = httpx.Response(
        200, json={"data": [{"id": 2, "documentId": "c1"}]}
    )
    authors = await provider.list_authors()
    categories = await provider.list_categories()
    assert authors[0]["documentId"] == "a1"
    assert categories[0]["documentId"] == "c1"


async def test_author_and_category_api_ids_are_configurable(fake):
    """M-3: the collection endpoints are configurable per deployment —
    a custom Strapi with ``writers`` / ``tags`` collection types is hit
    at ``/api/{custom}``, not the hardcoded ``authors`` / ``categories``.
    """
    settings = _settings()
    settings.strapi_author_plural_api_id = "writers"
    settings.strapi_category_plural_api_id = "tags"
    client = httpx.AsyncClient(
        base_url="http://strapi.test", transport=httpx.MockTransport(fake)
    )
    provider = StrapiCMSProvider(
        settings=settings, client=client, backoff_seconds=FAST_BACKOFF
    )
    authors = await provider.list_authors()
    categories = await provider.list_categories()
    assert authors == []
    assert categories == []
    # Both calls went to the configured endpoints, never the defaults.
    assert fake.calls("GET", "/api/writers")
    assert fake.calls("GET", "/api/tags")
    assert not fake.calls("GET", "/api/authors")
    assert not fake.calls("GET", "/api/categories")


# ---------------------------------------------------------------- slug search
async def test_find_blogs_by_slug_checks_both_statuses(fake, provider):
    fake.routes[("GET", "/api/blogs")] = httpx.Response(
        200, json={"data": [_blog_item(doc_id="doc-1")]}
    )
    entries = await provider.find_blogs_by_slug("my-slug")
    assert len(entries) == 2  # draft + published buckets
    slug_calls = fake.calls("GET", "/api/blogs")
    queries = {q(c) for c in slug_calls}
    assert any("status=draft" in q for q in queries)
    assert any("status=published" in q for q in queries)
    assert all("filters%5Bslug%5D" in q for q in queries)


# ------------------------------------------------------------------- draft CRUD
async def test_create_draft_entry_posts_with_status_draft(fake, provider):
    fake.routes[("POST", "/api/blogs")] = httpx.Response(
        201, json={"data": _blog_item(item_id=7, doc_id="doc-7")}
    )
    entry = await provider.create_draft_entry({"data": {"title": "T"}})
    assert entry.id == 7
    assert entry.document_id == "doc-7"
    req = fake.calls("POST", "/api/blogs")[0]
    assert "status=draft" in q(req)
    assert json.loads(req.content)["data"]["title"] == "T"


async def test_update_draft_entry_puts_with_status_draft(fake, provider):
    fake.routes[("PUT", "/api/blogs/doc-7")] = httpx.Response(
        200, json={"data": _blog_item(item_id=7, doc_id="doc-7")}
    )
    entry = await provider.update_draft_entry("doc-7", {"data": {"body": "x"}})
    assert entry.document_id == "doc-7"
    req = fake.calls("PUT", "/api/blogs/doc-7")[0]
    assert "status=draft" in q(req)


async def test_get_draft(fake, provider):
    fake.routes[("GET", "/api/blogs/doc-7")] = httpx.Response(
        200, json={"data": _blog_item(doc_id="doc-7")}
    )
    entry = await provider.get_draft("doc-7")
    assert entry.title == "T"
    req = fake.calls("GET", "/api/blogs/doc-7")[0]
    assert "status=draft" in q(req)


async def test_create_draft_failure_raises_code(fake, provider):
    fake.enqueue(httpx.Response(400, json={"error": {"message": "nope"}}))
    with pytest.raises(PipelineError) as excinfo:
        await provider.create_draft_entry({"data": {}})
    assert excinfo.value.error_code == ErrorCode.STRAPI_DRAFT_CREATE_FAILED


# -------------------------------------------------------------------- uploads
def _upload_response(url="/uploads/hero.webp", v4=False, wrapped=False):
    """``POST /api/upload`` response (H03, R-H02).

    The STANDARD Strapi upload controller response is a top-level ARRAY of
    file objects (``uploadFiles`` puts the list straight on ``ctx.body`` with
    HTTP 201). ``wrapped=True`` emulates a proxy/wrapper ``{"data": [...]}``
    and ``v4=True`` the legacy Strapi 4 single object — all three must work.
    """
    file_item = {
        "id": 11,
        "documentId": "doc-media-11",
        "url": url,
        "alternativeText": "alt",
    }
    if v4:
        return httpx.Response(201, json={"data": file_item})
    if wrapped:
        return httpx.Response(201, json={"data": [file_item]})
    return httpx.Response(201, json=[file_item])


def _multipart_fields(request: httpx.Request) -> dict:
    """Decode the multipart form fields of an upload request."""
    body = request.content.decode("utf-8")
    fields: dict = {}
    for part in body.split("\r\n--"):
        if "Content-Disposition: form-data; name=" not in part:
            continue
        header, _, content = part.partition("\r\n\r\n")
        name = header.split('name="')[1].split('"')[0]
        fields[name] = content.strip()
    return fields


async def test_upload_hero_multipart_fields(fake, provider):
    fake.routes[("POST", "/api/upload")] = _upload_response("/uploads/h.webp")
    result = await provider.upload_hero(
        b"img-bytes", "hero.webp", blog_numeric_id=7, alt_text="Hero alt"
    )
    assert result.media_id == 11
    assert result.url == "/uploads/h.webp"
    req = fake.calls("POST", "/api/upload")[0]
    fields = _multipart_fields(req)
    # section 38: ref = the configured Blog UID (never hardcoded in code)
    assert fields["ref"] == "api::blog.blog"
    assert fields["refId"] == "7"
    assert fields["field"] == "mainImage"
    info = json.loads(fields["fileInfo"])
    assert info["alternativeText"] == "Hero alt"
    assert info["name"] == "hero"
    # files part present
    assert b'filename="hero.webp"' in req.content
    assert b"img-bytes" in req.content
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"


async def test_upload_inline_has_no_entry_linking(fake, provider):
    fake.routes[("POST", "/api/upload")] = _upload_response()
    await provider.upload_inline(b"img", "inline-1.webp", alt_text="Alt")
    fields = _multipart_fields(fake.calls("POST", "/api/upload")[0])
    assert "ref" not in fields
    assert "refId" not in fields
    assert "field" not in fields


async def test_upload_failure_raises_code(fake, provider):
    # 500 is retryable: exhaust all 3 attempts
    for _ in range(3):
        fake.enqueue(httpx.Response(500, text="boom"))
    with pytest.raises(PipelineError) as excinfo:
        await provider.upload_inline(b"x", "a.webp")
    assert excinfo.value.error_code == ErrorCode.STRAPI_UPLOAD_FAILED
    assert "3 attempts" in excinfo.value.message


# --------------------------------------------------------------------- retry
async def test_retries_on_429_then_succeeds(fake, provider):
    fake.enqueue(httpx.Response(429, json={"error": {"message": "slow"}}))
    fake.routes[("POST", "/api/blogs")] = httpx.Response(
        201, json={"data": _blog_item(doc_id="doc-9")}
    )
    entry = await provider.create_draft_entry({"data": {}})
    assert entry.document_id == "doc-9"
    assert len(fake.calls("POST", "/api/blogs")) == 2


async def test_gives_up_after_three_attempts(fake, provider):
    fake.routes[("POST", "/api/blogs")] = httpx.Response(503, text="down")
    with pytest.raises(PipelineError) as excinfo:
        await provider.create_draft_entry({"data": {}})
    assert excinfo.value.error_code == ErrorCode.STRAPI_DRAFT_CREATE_FAILED
    assert len(fake.calls("POST", "/api/blogs")) == 3


async def test_401_fails_immediately_without_retry(fake, provider):
    fake.routes[("GET", "/api/blogs/doc-1")] = httpx.Response(
        401, json={"message": "invalid token"}
    )
    with pytest.raises(PipelineError) as excinfo:
        await provider.get_draft("doc-1")
    assert excinfo.value.error_code == ErrorCode.STRAPI_AUTH_FAILED
    # 401 is NOT retryable — exactly one request
    assert len(fake.calls("GET", "/api/blogs/doc-1")) == 1


# -------------------------------------------------------------------- security
async def test_token_never_in_error_messages(fake, provider):
    fake.routes[("POST", "/api/blogs")] = httpx.Response(
        400, json={"error": {"message": "field author invalid"}}
    )
    with pytest.raises(PipelineError) as excinfo:
        await provider.create_draft_entry({"data": {}})
    assert TOKEN not in str(excinfo.value)
    assert "Authorization" not in str(excinfo.value)


async def test_no_publish_or_delete_methods(provider):
    assert not hasattr(provider, "publish")
    assert not hasattr(provider, "delete")
    assert not hasattr(provider, "delete_draft")


async def test_404_is_not_retryable(fake, provider):
    fake.routes[("GET", "/api/blogs/nope")] = httpx.Response(404, text="gone")
    with pytest.raises(PipelineError):
        await provider.get_draft("nope")
    assert len(fake.calls("GET", "/api/blogs/nope")) == 1


async def test_schema_mismatch_on_bad_blog_response(fake, provider):
    # H03: flat item without id/documentId is a schema mismatch.
    fake.routes[("POST", "/api/blogs")] = httpx.Response(
        201, json={"data": {"title": "no ids"}}
    )
    with pytest.raises(PipelineError) as excinfo:
        await provider.create_draft_entry({"data": {}})
    assert excinfo.value.error_code == ErrorCode.STRAPI_SCHEMA_MISMATCH


# ------------------------------------------------------------------ H03 shape
async def test_get_draft_tolerates_legacy_v4_shape(fake, provider):
    """A Strapi 4 server (spec 35: repaired here only) still parses."""
    fake.routes[("GET", "/api/blogs/doc-7")] = httpx.Response(
        200, json={"data": _blog_item_v4(doc_id="doc-7", title="Legacy")}
    )
    entry = await provider.get_draft("doc-7")
    assert entry.title == "Legacy"
    assert entry.main_image == "https://cms/uploads/hero.webp"
    # and the request still populated the relation fields (M02)
    assert "populate%5Bauthor%5D" in q(
        fake.calls("GET", "/api/blogs/doc-7")[0]
    )


async def test_get_draft_parses_populated_relations(fake, provider):
    """H03/M02: relations come back as populated objects when the
    request used a ``populate`` query — the schema accepts them."""
    fake.routes[("GET", "/api/blogs/doc-7")] = httpx.Response(
        200,
        json={
            "data": _blog_item(
                doc_id="doc-7",
                author={
                    "id": 5,
                    "documentId": "doc-author-1",
                    "name": "Alice",
                },
                category={"id": 9, "documentId": "doc-cat-1", "name": "Health"},
                mainImage={
                    "id": 11,
                    "documentId": "doc-media-11",
                    "url": "/uploads/hero.webp",
                },
            )
        },
    )
    entry = await provider.get_draft("doc-7")
    assert entry.author == {
        "id": 5,
        "documentId": "doc-author-1",
        "name": "Alice",
    }
    assert entry.category["documentId"] == "doc-cat-1"
    assert entry.main_image["url"] == "/uploads/hero.webp"


async def test_upload_accepts_v4_single_object_response(fake, provider):
    """H03: the legacy v4 single-object upload response still works."""
    fake.routes[("POST", "/api/upload")] = _upload_response(v4=True)
    result = await provider.upload_inline(b"img", "inline-1.webp")
    assert result.media_id == 11
    assert result.url == "/uploads/hero.webp"


async def test_upload_array_missing_ids_is_schema_mismatch(fake, provider):
    fake.routes[("POST", "/api/upload")] = httpx.Response(
        201, json={"data": [{"url": "/uploads/x.webp"}]}
    )
    with pytest.raises(PipelineError) as excinfo:
        await provider.upload_inline(b"img", "inline-1.webp")
    assert excinfo.value.error_code == ErrorCode.STRAPI_SCHEMA_MISMATCH


# --------------------------------------------------- R-H02: standard top-level array
async def test_r_h02_upload_accepts_standard_top_level_array(fake, provider):
    """R-H02 (audit reproduction): the real Strapi response is a TOP-LEVEL
    array ``[{id,url,...}]`` — the old ``data.get('data')`` parser crashed with
    ``AttributeError: 'list' object has no attribute 'get'``."""
    fake.routes[("POST", "/api/upload")] = _upload_response("/uploads/hero.webp")
    result = await provider.upload_inline(b"img", "inline-1.webp", alt_text="Alt")
    assert result.media_id == 11
    assert result.url == "/uploads/hero.webp"
    assert result.document_id == "doc-media-11"


async def test_r_h02_upload_accepts_wrapped_data_array(fake, provider):
    """Proxy/wrapper compatibility: ``{"data": [...]}`` still parses."""
    fake.routes[("POST", "/api/upload")] = _upload_response(
        "/uploads/w.webp", wrapped=True
    )
    result = await provider.upload_inline(b"img", "inline-1.webp")
    assert result.media_id == 11
    assert result.url == "/uploads/w.webp"


@pytest.mark.parametrize(
    "payload",
    [[], {}, {"data": []}, {"data": None}, "not-a-payload", None],
    ids=["empty-list", "empty-dict", "empty-data-list", "null-data", "string", "null"],
)
async def test_r_h02_upload_empty_or_malformed_is_schema_mismatch(
    fake, provider, payload
):
    """Empty/malformed upload bodies become a stable PipelineError — never a
    raw AttributeError/TypeError."""
    fake.routes[("POST", "/api/upload")] = httpx.Response(201, json=payload)
    with pytest.raises(PipelineError) as excinfo:
        await provider.upload_inline(b"img", "inline-1.webp")
    # both are stable PipelineError codes: unparseable body -> UPLOAD_FAILED,
    # parseable but empty/malformed structure -> SCHEMA_MISMATCH.
    assert excinfo.value.error_code in (
        ErrorCode.STRAPI_SCHEMA_MISMATCH,
        ErrorCode.STRAPI_UPLOAD_FAILED,
    )
