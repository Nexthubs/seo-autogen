"""Tavily content extractor + primary fallback (P9-A — spec section 10.3/15,
P9 optional "Tavily extractor fallback").

The primary extractor stays Exa. When ``TAVILY_API_KEY`` is configured,
:func:`build_extractor` returns a :class:`FallingBackExtractor`: if the
primary fails for a URL (or the whole primary call), that URL is retried
against the Tavily Extract API. Both providers failing leaves the URL to the
existing per-URL skip-and-backfill (spec section 14.1). Without a Tavily key
the plain primary is returned unchanged — the fallback is strictly optional.

Tavily Extract API:

    POST /extract
    {"urls": ["https://..."]}

Response (documented shape):

    {"results": [{"url": "...", "raw_content": "..."}],
     "failed_results": [{"url": "...", "error": "..."}]}
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import ErrorCode, PipelineError
from app.providers.extractor.base import ContentExtractor
from app.providers.extractor.exa import truncate_content
from app.schemas.sources import ExtractedPage
from app.services.url_normalizer import normalize_url

logger = logging.getLogger(__name__)

ENDPOINT = "/extract"

EXTRACTOR_NAME = "tavily"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
NON_RETRYABLE_STATUS = {400, 401, 403, 404}
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (2.0, 5.0, 15.0)


class TavilyContentExtractor(ContentExtractor):
    """ContentExtractor backed by the Tavily /extract API."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        backoff_seconds: tuple[float, ...] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._backoff_seconds = backoff_seconds or RETRY_BACKOFF_SECONDS
        self._client = client or httpx.AsyncClient(
            base_url=self._settings.tavily_base_url,
            headers={"Authorization": f"Bearer {self._settings.tavily_api_key}"},
            timeout=httpx.Timeout(self._settings.extractor_timeout_seconds),
        )
        self._owns_client = client is None

    # ------------------------------------------------------------
    async def _post_with_retry(self, payload: dict) -> dict:
        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await self._client.post(ENDPOINT, json=payload)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                logger.warning(
                    "tavily_request_failed",
                    extra={
                        "event": "tavily_request_failed",
                        "error_code": "TIMEOUT_OR_NETWORK",
                        "attempt": attempt + 1,
                    },
                )
                await self._backoff(attempt)
                continue

            if response.status_code == 200:
                return response.json()

            if response.status_code in NON_RETRYABLE_STATUS:
                code = (
                    ErrorCode.EXTRACTOR_AUTH_FAILED
                    if response.status_code in (401, 403)
                    else ErrorCode.EXTRACTOR_FAILED
                )
                raise PipelineError(
                    code,
                    f"Tavily returned {response.status_code}",
                    raw=response.text[:5000],
                )

            last_error = PipelineError(
                ErrorCode.EXTRACTOR_FAILED,
                f"Tavily returned {response.status_code}",
                raw=response.text[:5000],
            )
            logger.warning(
                "tavily_request_failed",
                extra={
                    "event": "tavily_request_failed",
                    "error_code": f"HTTP_{response.status_code}",
                    "attempt": attempt + 1,
                },
            )
            await self._backoff(attempt)

        raise PipelineError(
            ErrorCode.EXTRACTOR_FAILED,
            f"Tavily request failed after {MAX_ATTEMPTS} attempts",
            raw=str(last_error) if last_error else None,
        )

    async def _backoff(self, attempt: int) -> None:
        if attempt < len(self._backoff_seconds):
            await asyncio.sleep(self._backoff_seconds[attempt])

    # ------------------------------------------------------------
    # ContentExtractor interface
    # ------------------------------------------------------------
    async def extract(self, urls: list[str]) -> list[ExtractedPage]:
        """Extract content for the given URLs via Tavily.

        Per-URL failures are skipped after a warning (same contract as the
        Exa extractor; the step backfills from the next organic result).
        """
        if not urls:
            raise PipelineError(
                ErrorCode.SOURCE_EMPTY, "extract() received no urls"
            )

        data = await self._post_with_retry({"urls": urls})
        results = data.get("results") or []
        failed_urls = {
            f.get("url") for f in data.get("failed_results") or []
            if isinstance(f, dict)
        }

        now = datetime.now(timezone.utc)
        pages: list[ExtractedPage] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not url or url in failed_urls:
                logger.warning(
                    "tavily_page_failed",
                    extra={"event": "tavily_page_failed", "url": url},
                )
                continue
            text = (item.get("raw_content") or "").strip()
            if not text:
                logger.warning(
                    "tavily_page_empty",
                    extra={"event": "tavily_page_empty", "url": url},
                )
                continue
            pages.append(
                ExtractedPage(
                    url=url,
                    normalized_url=normalize_url(url),
                    title=item.get("title"),
                    content_markdown=truncate_content(
                        text, self._settings.source_max_chars
                    ),
                    extracted_at=now,
                    extractor=EXTRACTOR_NAME,
                )
            )

        if not pages:
            raise PipelineError(
                ErrorCode.SOURCE_EMPTY,
                "Tavily returned no usable content for any url",
                raw=str(data)[:5000],
            )
        return pages

    async def health_check(self) -> bool:
        """Verify the configured Tavily key passes auth (paid, single URL).

        Tavily has no documented free health endpoint, so this probes the
        real ``/extract`` API with one URL. Unlike the previous
        ``status_code < 500`` check, it strictly requires a business
        success: an HTTP 200 whose body is the documented
        ``{"results": [...], "failed_results": [...]}`` shape. A 401/403
        (invalid key) or any other non-200 / malformed body reports False,
        so a bad key is never surfaced as Connected (audit M15).

        Cost trade-off: each probe performs one real extraction (one credit).
        """
        if not self._settings.tavily_api_key:
            return False
        try:
            response = await self._client.post(
                ENDPOINT, json={"urls": ["https://example.com"]}
            )
        except httpx.HTTPError:
            return False
        if response.status_code != 200:
            logger.info(
                "tavily_health_check_failed",
                extra={
                    "event": "tavily_health_check_failed",
                    "status_code": response.status_code,
                },
            )
            return False
        try:
            body = response.json()
        except ValueError:
            return False
        if not isinstance(body, dict):
            return False
        # Both keys must be present and lists (a real extract response).
        if (
            isinstance(body.get("results"), list)
            and isinstance(body.get("failed_results"), list)
        ):
            return True
        logger.info(
            "tavily_health_check_failed",
            extra={
                "event": "tavily_health_check_failed",
                "status_code": 200,
                "malformed_body": True,
            },
        )
        return False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class FallingBackExtractor(ContentExtractor):
    """Primary extractor with a per-URL fallback to a second provider.

    The primary is called for the full URL list. When it fails outright, or
    when it comes back missing one or more URLs, the missing URLs are
    retried against the fallback provider. If the fallback fails too, those
    URLs stay missing — the source step's existing skip-and-backfill then
    applies (spec section 14.1: a failed URL never kills the pipeline).
    """

    def __init__(
        self,
        primary: ContentExtractor,
        fallback: ContentExtractor,
    ) -> None:
        self._primary = primary
        self._fallback = fallback

    async def extract(self, urls: list[str]) -> list[ExtractedPage]:
        if not urls:
            raise PipelineError(
                ErrorCode.SOURCE_EMPTY, "extract() received no urls"
            )

        pages: list[ExtractedPage] = []
        try:
            pages = await self._primary.extract(list(urls))
        except PipelineError as error:
            logger.warning(
                "extractor_primary_failed",
                extra={
                    "event": "extractor_primary_failed",
                    "error_code": error.error_code.value,
                    "urls": len(urls),
                },
            )
            pages = []

        got = {p.normalized_url for p in pages}
        missing = [u for u in urls if normalize_url(u) not in got]
        if missing:
            try:
                for page in await self._fallback.extract(missing):
                    logger.info(
                        "extractor_fallback_used",
                        extra={
                            "event": "extractor_fallback_used",
                            "url": page.normalized_url,
                        },
                    )
                    pages.append(page)
            except PipelineError as error:
                # Both providers failed for these URLs: the step's
                # skip-and-backfill handles the gap (spec 14.1).
                logger.warning(
                    "extractor_fallback_failed",
                    extra={
                        "event": "extractor_fallback_failed",
                        "error_code": error.error_code.value,
                        "urls": len(missing),
                    },
                )
            except Exception:  # noqa: BLE001 - fallback must never mask the primary
                logger.exception(
                    "extractor_fallback_crash",
                    extra={"event": "extractor_fallback_crash"},
                )

        if not pages:
            raise PipelineError(
                ErrorCode.SOURCE_EMPTY,
                "primary and fallback extractors returned no usable content",
            )
        return pages

    async def health_check(self) -> bool:
        """Primary healthy, or (fallback) configured — either can carry a URL."""
        return await self._primary.health_check() or await self._fallback.health_check()

    async def aclose(self) -> None:
        for provider in (self._primary, self._fallback):
            aclose = getattr(provider, "aclose", None)
            if aclose is None:
                continue
            try:
                await aclose()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "extractor_aclose_failed",
                    extra={"event": "extractor_aclose_failed"},
                )


def build_extractor(settings: Settings | None = None) -> ContentExtractor:
    """The configured extractor (P9-A-6).

    Exa is always the primary. When ``TAVILY_API_KEY`` is set the primary is
    wrapped in a :class:`FallingBackExtractor`; otherwise the plain primary
    is returned (the fallback is optional by design).
    """
    settings = settings or get_settings()
    from app.providers.extractor.exa import ExaContentExtractor

    primary = ExaContentExtractor(settings=settings)
    if settings.tavily_api_key:
        return FallingBackExtractor(
            primary, TavilyContentExtractor(settings=settings)
        )
    return primary
