"""StrapiCMSProvider — Strapi 5 Draft-level CMS access
(SEO-AUTO-DEV-SPEC.md sections 35-42, 51, 52, 53, 60, P7).

SAFETY CONSTRAINTS (spec section 35.1 / 60):
  - EVERY Blog write (POST create / PUT update) explicitly targets
    ``?status=draft``. The code never relies on Strapi's default.
  - There is NO ``publish()`` and NO ``delete()`` method. Publishing
    is manual in V1; the only deletion in the system is the
    integration-test cleanup (section 59.3), which calls the HTTP
    DELETE directly in that test — it is NOT part of this provider.

Retry policy (spec section 51, unified HTTP retry):
    retryable      429, 500, 502, 503, 504, timeout, connection reset
    non-retryable  400, 401, 403, 404 (configuration errors)
    backoff        2s -> 5s -> 15s (max 3 attempts)

Security (spec section 60 / 53):
    The Bearer token is sent per request (injected clients do not
    inherit client-level headers — P6 lesson) but NEVER appears in
    logs or error messages; HTTP error bodies are truncated and
    contain no headers.
"""

import asyncio
import json
import logging

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import ErrorCode, PipelineError
from app.providers.cms.base import CMSProvider
from app.schemas.article import ArticleDocument
from app.schemas.strapi import (
    StrapiBlogEntry,
    MediaUploadResult,
    build_draft_payload,
)

logger = logging.getLogger(__name__)

#: Spec section 51: backoff 2s / 5s / 15s, max 3 attempts.
RETRY_BACKOFF_SECONDS = (2.0, 5.0, 15.0)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class StrapiCMSProvider(CMSProvider):
    """CMSProvider backed by a Strapi 5 REST API (V1, Draft-only)."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        backoff_seconds: tuple[float, ...] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._backoff_seconds = backoff_seconds or RETRY_BACKOFF_SECONDS
        self._client = client or httpx.AsyncClient(
            base_url=self._settings.strapi_base_url,
            timeout=httpx.Timeout(self._settings.strapi_timeout_seconds),
        )
        self._owns_client = client is None
        self._auth = f"Bearer {self._settings.strapi_api_token}"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------
    # Schema discovery / health (P7 "先做 Schema Discovery")
    # ------------------------------------------------------------
    async def health_check(self) -> bool:
        """True when the Blog endpoint is reachable AND the token has
        Blog read permission (``GET /api/{pluralApiId}``, section 35).
        """
        try:
            data = await self._get(
                f"/api/{self._settings.strapi_blog_plural_api_id}",
                {"pagination[pageSize]": 1},
                code=ErrorCode.STRAPI_AUTH_FAILED,
            )
            return isinstance(data.get("data"), list)
        except PipelineError:
            return False

    # ------------------------------------------------------------
    # List Authors / List Categories (P7)
    # ------------------------------------------------------------
    async def list_authors(self, page_size: int = 10) -> list[dict]:
        data = await self._get(
            f"/api/{self._settings.strapi_author_plural_api_id}",
            {"pagination[pageSize]": page_size},
            code=ErrorCode.STRAPI_AUTH_FAILED,
        )
        return data.get("data") or []

    async def list_categories(self, page_size: int = 10) -> list[dict]:
        data = await self._get(
            f"/api/{self._settings.strapi_category_plural_api_id}",
            {"pagination[pageSize]": page_size},
            code=ErrorCode.STRAPI_AUTH_FAILED,
        )
        return data.get("data") or []

    # ------------------------------------------------------------
    # Slug collision (section 42)
    # ------------------------------------------------------------
    async def find_blogs_by_slug(self, slug: str) -> list[StrapiBlogEntry]:
        """All Draft AND published Blog entries with this slug.

        Section 42: both status buckets must be checked before sync.
        """
        entries: list[StrapiBlogEntry] = []
        for status in ("draft", "published"):
            data = await self._get(
                f"/api/{self._settings.strapi_blog_plural_api_id}",
                {
                    "filters[slug][$eq]": slug,
                    "status": status,
                    "pagination[pageSize]": 10,
                },
                code=ErrorCode.STRAPI_AUTH_FAILED,
            )
            for item in data.get("data") or []:
                entries.append(self._entry_from_item(item))
        return entries

    # ------------------------------------------------------------
    # Draft entry-level API (sections 35.1, 37, 40)
    # ------------------------------------------------------------
    async def create_draft_entry(self, payload: dict) -> StrapiBlogEntry:
        """``POST /api/{pluralApiId}?status=draft`` (section 37).

        ``payload`` is the ``{"data": {...}}`` body built by
        ``build_draft_payload``. Returns the created entry (numeric
        id + Strapi 5 short documentId).
        """
        data = await self._request(
            "POST",
            f"/api/{self._settings.strapi_blog_plural_api_id}",
            params={"status": "draft"},  # section 35.1: explicit
            json_body=payload,
            code=ErrorCode.STRAPI_DRAFT_CREATE_FAILED,
        )
        return self._entry_from_item(data.get("data") or {})

    async def update_draft_entry(
        self, document_id: str, payload: dict
    ) -> StrapiBlogEntry:
        """``PUT /api/{pluralApiId}/{documentId}?status=draft``
        (sections 35.1, 40, 41: idempotent re-sync updates)."""
        data = await self._request(
            "PUT",
            f"/api/{self._settings.strapi_blog_plural_api_id}/{document_id}",
            params={"status": "draft"},  # section 35.1: explicit
            json_body=payload,
            code=ErrorCode.STRAPI_DRAFT_UPDATE_FAILED,
        )
        return self._entry_from_item(data.get("data") or {})

    async def get_draft(self, document_id: str) -> StrapiBlogEntry:
        """``GET /api/{pluralApiId}/{documentId}?status=draft``
        (STEP F verification, section 36)."""
        data = await self._get(
            f"/api/{self._settings.strapi_blog_plural_api_id}/{document_id}",
            {"status": "draft"},
            code=ErrorCode.STRAPI_AUTH_FAILED,
        )
        return self._entry_from_item(data.get("data") or {})

    # ------------------------------------------------------------
    # Media upload (sections 38, 39)
    # ------------------------------------------------------------
    async def upload_hero(
        self,
        file_bytes: bytes,
        filename: str,
        *,
        blog_numeric_id: int,
        alt_text: str = "",
        caption: str = "",
    ) -> MediaUploadResult:
        """Hero upload bound to ``mainImage`` via entry linking
        (section 38): multipart ``files`` + ``ref`` (the configured
        Blog UID, never hardcoded) + ``refId`` (numeric id) +
        ``field=mainImage`` + JSON ``fileInfo``."""
        return await self._upload(
            file_bytes,
            filename,
            ref=self._settings.strapi_blog_uid,
            ref_id=str(blog_numeric_id),
            field="mainImage",
            alt_text=alt_text,
            caption=caption,
        )

    async def upload_inline(
        self,
        file_bytes: bytes,
        filename: str,
        *,
        alt_text: str = "",
    ) -> MediaUploadResult:
        """Inline upload: plain ``POST /api/upload`` (section 39)."""
        return await self._upload(file_bytes, filename, alt_text=alt_text)

    # ------------------------------------------------------------
    # CMSProvider V1 interface (section 10.5 / 35)
    # ------------------------------------------------------------
    async def create_draft(self, article: ArticleDocument) -> str:
        """Convenience wrapper around ``create_draft_entry`` using the
        configured defaults for author/category; returns documentId."""
        payload = build_draft_payload(
            article,
            author_document_id=self._settings.strapi_default_author_document_id,
            category_document_id=self._settings.strapi_default_category_document_id,
            body=article.body_markdown,
            include_posted_at=self._settings.strapi_set_posted_at_on_draft,
        )
        entry = await self.create_draft_entry(payload)
        return entry.document_id

    async def update_draft(self, document_id: str, article: ArticleDocument) -> None:
        """Convenience wrapper: final-body update (section 40)."""
        await self.update_draft_entry(
            document_id, {"data": {"body": article.body_markdown}}
        )

    async def upload_media(
        self, *, path: str, alt_text: str = ""
    ) -> MediaUploadResult:
        """Local file upload (inline variant of section 39)."""
        with open(path, "rb") as fh:
            data = fh.read()
        filename = path.rsplit("/", 1)[-1]
        return await self._upload(data, filename, alt_text=alt_text)

    # ------------------------------------------------------------
    # internals
    # ------------------------------------------------------------
    def _entry_from_item(self, item: dict) -> StrapiBlogEntry:
        attributes = item.get("attributes") or {}
        if "id" not in item or "documentId" not in item:
            raise PipelineError(
                ErrorCode.STRAPI_SCHEMA_MISMATCH,
                "Strapi Blog response lacks id/documentId",
                raw=json.dumps(item, ensure_ascii=False)[:300],
            )
        return StrapiBlogEntry(
            id=item["id"],
            document_id=item["documentId"],
            slug=attributes.get("slug"),
            title=attributes.get("title"),
            status=attributes.get("status"),
            body=attributes.get("body"),
            meta_title=attributes.get("metaTitle"),
            meta_description=attributes.get("metaDescription"),
            seo_keywords=attributes.get("seoKeywords"),
            author=attributes.get("author"),
            category=attributes.get("category"),
            main_image=attributes.get("mainImage"),
        )

    async def _upload(
        self,
        file_bytes: bytes,
        filename: str,
        *,
        alt_text: str,
        caption: str = "",
        ref: str | None = None,
        ref_id: str | None = None,
        field: str | None = None,
    ) -> MediaUploadResult:
        form: dict = {
            "fileInfo": json.dumps(
                {
                    "name": filename.rsplit(".", 1)[0],
                    "alternativeText": alt_text or "",
                    "caption": caption or "",
                },
                ensure_ascii=False,
            ),
        }
        if ref is not None:
            form["ref"] = ref
        if ref_id is not None:
            form["refId"] = ref_id
        if field is not None:
            form["field"] = field

        attempts = 3
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(self._backoff_seconds[min(attempt, 2)])
            try:
                response = await self._client.post(
                    "/api/upload",
                    data=form,
                    files={"files": (filename, file_bytes)},
                    headers={"Authorization": self._auth},
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                logger.warning(
                    "strapi_upload_failed",
                    extra={
                        "event": "strapi_upload_failed",
                        "filename": filename,
                        "attempt": attempt + 1,
                        "error": str(exc),
                    },
                )
                continue
            if response.status_code in RETRYABLE_STATUS:
                continue
            if response.status_code in (401, 403):
                # Section 60: no token in the message — status only.
                raise PipelineError(
                    ErrorCode.STRAPI_AUTH_FAILED,
                    f"Strapi upload HTTP {response.status_code}: token "
                    "rejected or insufficient permissions",
                )
            if response.status_code not in (200, 201):
                raise PipelineError(
                    ErrorCode.STRAPI_UPLOAD_FAILED,
                    f"upload HTTP {response.status_code}: "
                    f"{response.text[:300]}",
                )
            try:
                data = response.json()
            except ValueError as exc:
                raise PipelineError(
                    ErrorCode.STRAPI_UPLOAD_FAILED,
                    "upload returned non-JSON body",
                    raw=response.text[:300],
                ) from exc
            return self._upload_result(data)
        raise PipelineError(
            ErrorCode.STRAPI_UPLOAD_FAILED,
            f"upload failed after {attempts} attempts",
        )

    @staticmethod
    def _upload_result(data: dict) -> MediaUploadResult:
        item = (data.get("data") or {}).get("data") or {}
        if not item.get("id") or not item.get("url"):
            raise PipelineError(
                ErrorCode.STRAPI_SCHEMA_MISMATCH,
                "Strapi upload response lacks id/url",
                raw=json.dumps(data, ensure_ascii=False)[:300],
            )
        return MediaUploadResult(
            media_id=item["id"],
            url=item["url"],
            document_id=item.get("documentId"),
            alternative_text=(item.get("alternativeText") or None),
        )

    async def _get(self, path: str, params: dict, *, code: ErrorCode) -> dict:
        return await self._request("GET", path, params=params, code=code)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        code: ErrorCode,
    ) -> dict:
        """One JSON request with the section-51 retry policy.

        The Authorization header is attached per request (injected
        MockTransport clients do not inherit client-level headers)
        and never logged (sections 53, 60).
        """
        attempts = 3
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(self._backoff_seconds[min(attempt, 2)])
            try:
                response = await self._client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    headers={"Authorization": self._auth},
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                logger.warning(
                    "strapi_request_failed",
                    extra={
                        "event": "strapi_request_failed",
                        "path": path,
                        "attempt": attempt + 1,
                        "error": str(exc),
                    },
                )
                continue
            if response.status_code in RETRYABLE_STATUS:
                continue
            if response.status_code in (401, 403):
                # Section 60: no token in the message — status only.
                raise PipelineError(
                    code,
                    f"Strapi HTTP {response.status_code}: token "
                    "rejected or insufficient permissions",
                )
            if response.status_code not in (200, 201):
                # Strapi v4 answers 201 Created on POST /api/{model}.
                raise PipelineError(
                    code,
                    f"Strapi HTTP {response.status_code} "
                    f"({method} {path}): {response.text[:300]}",
                )
            try:
                return response.json()
            except ValueError as exc:
                raise PipelineError(
                    code,
                    "Strapi returned non-JSON body",
                    raw=response.text[:300],
                ) from exc
        raise PipelineError(
            code,
            f"Strapi failed after {attempts} attempts "
            f"({method} {path})",
        )
