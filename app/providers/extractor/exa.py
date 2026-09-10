"""Exa content extractor (SEO-AUTO-DEV-SPEC.md sections 10.3, 15).

Exa receives ONLY URLs that came from DataForSEO (spec section 15: it
must never decide search ranking itself). The canonical endpoint is the
``/contents`` API:

    POST /contents
    Authorization: x-api-key <EXA_API_KEY>
    {"ids": ["https://...", ...], "text": true}

    ``text`` MUST be sent explicitly: it defaults to ``false`` on the API,
    in which case no page body is returned and every result is empty (the
    step would raise SOURCE_EMPTY).

Response (documented example):

    {
      "results": [
        {"id": "...", "title": "...", "url": "...", "text": "..."}
      ],
      "statuses": [{"id": "...", "status": "success", "source": "cached"}],
      "costDollars": {"total": 0.003}
    }

Deterministic truncation (spec section 15): content longer than
``SOURCE_MAX_CHARS`` is cut from the tail at the same character budget
every time — never random.

Retry policy (spec section 51): 429/5xx/timeout retried with
2s/5s/15s backoff; 401/403 -> EXTRACTOR_AUTH_FAILED, 400/404 ->
EXTRACTOR_FAILED (no retry).
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import ErrorCode, PipelineError
from app.providers.extractor.base import ContentExtractor
from app.schemas.sources import ExtractedPage
from app.services.url_normalizer import normalize_url

logger = logging.getLogger(__name__)

ENDPOINT = "/contents"

RETRY_BACKOFF_SECONDS = (2.0, 5.0, 15.0)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
NON_RETRYABLE_STATUS = {400, 401, 403, 404}
MAX_ATTEMPTS = 3

EXTRACTOR_NAME = "exa"


def truncate_content(content: str, max_chars: int) -> str:
    """Deterministic truncation (spec section 15).

    Cuts at the character budget; the cut never moves between runs for
    the same input and budget.
    """
    if len(content) <= max_chars:
        return content
    return content[:max_chars]


class ExaContentExtractor(ContentExtractor):
    """ContentExtractor backed by the Exa /contents API."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        backoff_seconds: tuple[float, ...] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._backoff_seconds = backoff_seconds or RETRY_BACKOFF_SECONDS
        self._client = client or httpx.AsyncClient(
            base_url=self._settings.exa_base_url,
            headers={"x-api-key": self._settings.exa_api_key},
            timeout=httpx.Timeout(self._settings.extractor_timeout_seconds),
        )
        self._owns_client = client is None

    # ------------------------------------------------------------
    async def _post_with_retry(self, payload: dict) -> dict:
        last_error: Exception | None = None
        started = time.monotonic()

        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await self._client.post(ENDPOINT, json=payload)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                logger.warning(
                    "exa_request_failed",
                    extra={
                        "event": "exa_request_failed",
                        "error_code": "TIMEOUT_OR_NETWORK",
                        "attempt": attempt + 1,
                    },
                )
                await self._backoff(attempt)
                continue

            if response.status_code == 200:
                logger.info(
                    "exa_request_ok",
                    extra={
                        "event": "exa_request_ok",
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "attempts": attempt + 1,
                    },
                )
                return response.json()

            if response.status_code in NON_RETRYABLE_STATUS:
                code = (
                    ErrorCode.EXTRACTOR_AUTH_FAILED
                    if response.status_code in (401, 403)
                    else ErrorCode.EXTRACTOR_FAILED
                )
                raise PipelineError(
                    code,
                    f"Exa returned {response.status_code}",
                    raw=response.text[:5000],
                )

            last_error = PipelineError(
                ErrorCode.EXTRACTOR_FAILED,
                f"Exa returned {response.status_code}",
                raw=response.text[:5000],
            )
            logger.warning(
                "exa_request_failed",
                extra={
                    "event": "exa_request_failed",
                    "error_code": f"HTTP_{response.status_code}",
                    "attempt": attempt + 1,
                },
            )
            await self._backoff(attempt)

        raise PipelineError(
            ErrorCode.EXTRACTOR_FAILED,
            f"Exa request failed after {MAX_ATTEMPTS} attempts",
            raw=str(last_error) if last_error else None,
        )

    async def _backoff(self, attempt: int) -> None:
        if attempt < len(self._backoff_seconds):
            await asyncio.sleep(self._backoff_seconds[attempt])

    # ------------------------------------------------------------
    # ContentExtractor interface
    # ------------------------------------------------------------
    async def extract(self, urls: list[str]) -> list[ExtractedPage]:
        """Extract content for the given URLs (spec section 15).

        URLs failing per-page (status != success, empty text) are
        skipped after a warning. When no URL yields content the step
        raises SOURCE_EMPTY.
        """
        if not urls:
            raise PipelineError(
                ErrorCode.SOURCE_EMPTY, "extract() received no urls"
            )

        # ``text`` is sent explicitly: the API defaults it to ``false``,
        # which would return no page bodies and cause a SOURCE_EMPTY.
        data = await self._post_with_retry({"ids": urls, "text": True})
        # Batch cost (spec section 54): Exa reports a per-REQUEST total in
        # ``costDollars.total``. The pipeline extracts one URL per call, so
        # the total IS the per-page cost; for a hypothetical multi-URL batch
        # it is still the best available attribution (per-page prices are
        # not returned).
        batch_cost: float | None = None
        cost_dollars = data.get("costDollars")
        if isinstance(cost_dollars, dict):
            total = cost_dollars.get("total")
            if isinstance(total, (int, float)) and not isinstance(total, bool):
                batch_cost = float(total)
        results = data.get("results") or []
        statuses = data.get("statuses") or []
        failed_ids = {
            s.get("id")
            for s in statuses
            if isinstance(s, dict) and s.get("status") != "success"
        }

        now = datetime.now(timezone.utc)
        pages: list[ExtractedPage] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or item.get("id")
            if not url or url in failed_ids:
                logger.warning(
                    "exa_page_failed",
                    extra={"event": "exa_page_failed", "url": url},
                )
                continue
            text = (item.get("text") or "").strip()
            if not text:
                logger.warning(
                    "exa_page_empty",
                    extra={"event": "exa_page_empty", "url": url},
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
                    provider_cost=batch_cost,
                )
            )

        if not pages:
            raise PipelineError(
                ErrorCode.SOURCE_EMPTY,
                "Exa returned no usable content for any url",
                raw=data,
            )
        return pages

    async def health_check(self) -> bool:
        """Verify the configured Exa key passes auth (zero cost).

        Exa validates the ``x-api-key`` header *before* it processes the
        request body, so a POST to ``/search`` with a body that is
        intentionally invalid (missing the required ``query``) is rejected
        on authentication and never executed — nothing is billed:

            - invalid / missing key -> 401/403  -> unhealthy
            - valid key, bad body   -> 400      -> healthy (key was accepted)

        This is a free auth probe that strictly distinguishes auth failures,
        so a bad key is never reported as Connected (audit M15). The previous
        implementation hit the undocumented ``GET /status`` and treated any
        sub-500 response (including 401/403/404) as healthy.
        """
        if not self._settings.exa_configured:
            return False
        try:
            response = await self._client.post("/search", json={})
        except httpx.HTTPError:
            return False
        if response.status_code in (401, 403):
            logger.info(
                "exa_health_check_failed",
                extra={
                    "event": "exa_health_check_failed",
                    "status_code": response.status_code,
                },
            )
            return False
        # A valid key reaches the API and is rejected only because the probe
        # body is invalid.
        return response.status_code in (400, 422)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
