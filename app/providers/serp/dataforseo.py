"""DataForSEO SERP provider (SEO-AUTO-DEV-SPEC.md sections 10.2, 12, 51).

Endpoint:
    POST /v3/serp/google/organic/live/advanced
    HTTP Basic Auth with DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD

The request body is a JSON *array* of one task. Only the `advanced`
SERP features we actually store are requested:

    organic, people_also_ask, related_searches, featured_snippet

(``people_also_ask_click_depth: 1`` is added only when
``DATAFORSEO_PAA_CLICK_DEPTH > 0``.)

The full raw response must be preserved (spec section 12.2) — callers
store it in ``serp_runs.raw_response``.

Retry policy (spec section 51):
    retryable      429, 500, 502, 503, 504, timeout, connection reset
    non-retryable  400, 401, 403, 404 (configuration errors)
    backoff        2s -> 5s -> 15s, max 3 attempts
"""

import asyncio
import logging
import time

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import ErrorCode, PipelineError
from app.providers.serp.base import SERPProvider
from app.schemas.serp import (
    OrganicResult,
    PAAQuestion,
    SERPRequest,
    SERPResponse,
)

logger = logging.getLogger(__name__)

ENDPOINT = "/v3/serp/google/organic/live/advanced"

RETRY_BACKOFF_SECONDS = (2.0, 5.0, 15.0)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
NON_RETRYABLE_STATUS = {400, 401, 403, 404}
MAX_ATTEMPTS = 3


class DataForSEOSERPProvider(SERPProvider):
    """SERPProvider backed by the DataForSEO Google Organic Advanced API."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        backoff_seconds: tuple[float, ...] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._backoff_seconds = backoff_seconds or RETRY_BACKOFF_SECONDS
        self._client = client or httpx.AsyncClient(
            base_url=self._settings.dataforseo_base_url,
            auth=(
                self._settings.dataforseo_login,
                self._settings.dataforseo_password,
            ),
            timeout=httpx.Timeout(self._settings.serp_timeout_seconds),
        )
        self._owns_client = client is None

    # ------------------------------------------------------------
    # request building (spec section 12)
    # ------------------------------------------------------------
    def build_task(self, request: SERPRequest) -> dict:
        s = self._settings
        task: dict = {
            "keyword": request.keyword,
            "location_code": request.location_code
            or s.dataforseo_location_code,
            "language_code": request.language_code or s.dataforseo_language_code,
            "device": request.device or s.dataforseo_device,
            "depth": request.depth or s.dataforseo_depth,
            "calculate_rectangles": s.dataforseo_calculate_rectangles,
            "load_async_ai_overview": s.dataforseo_load_async_ai_overview,
        }
        if s.dataforseo_paa_click_depth > 0:
            task["people_also_ask_click_depth"] = s.dataforseo_paa_click_depth
        return task

    # ------------------------------------------------------------
    # low-level request with the unified retry policy
    # ------------------------------------------------------------
    async def _request_raw(self, request: SERPRequest) -> tuple[dict, float | None]:
        """Return (raw response body, cost) after the unified retry policy."""
        payload = [self.build_task(request)]
        last_error: Exception | None = None
        started = time.monotonic()

        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await self._client.post(ENDPOINT, json=payload)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                logger.warning(
                    "dataforseo_request_failed",
                    extra={
                        "event": "dataforseo_request_failed",
                        "error_code": "TIMEOUT_OR_NETWORK",
                        "attempt": attempt + 1,
                    },
                )
                await self._backoff(attempt)
                continue

            if response.status_code == 200:
                data = response.json()
                logger.info(
                    "dataforseo_request_ok",
                    extra={
                        "event": "dataforseo_request_ok",
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "attempts": attempt + 1,
                    },
                )
                return data, self._extract_cost(data)

            if response.status_code in NON_RETRYABLE_STATUS:
                code = (
                    ErrorCode.DATAFORSEO_AUTH_FAILED
                    if response.status_code in (401, 403)
                    else ErrorCode.DATAFORSEO_REQUEST_FAILED
                )
                raise PipelineError(
                    code,
                    f"DataForSEO returned {response.status_code}",
                    raw=response.text[:5000],
                )

            # 429 / 5xx -> retryable.
            last_error = PipelineError(
                ErrorCode.DATAFORSEO_REQUEST_FAILED,
                f"DataForSEO returned {response.status_code}",
                raw=response.text[:5000],
            )
            logger.warning(
                "dataforseo_request_failed",
                extra={
                    "event": "dataforseo_request_failed",
                    "error_code": f"HTTP_{response.status_code}",
                    "attempt": attempt + 1,
                },
            )
            await self._backoff(attempt)

        raise PipelineError(
            ErrorCode.DATAFORSEO_REQUEST_FAILED,
            f"DataForSEO request failed after {MAX_ATTEMPTS} attempts",
            raw=str(last_error) if last_error else None,
        )

    async def _backoff(self, attempt: int) -> None:
        if attempt < len(self._backoff_seconds):
            await asyncio.sleep(self._backoff_seconds[attempt])

    # ------------------------------------------------------------
    # parsing (defensive: DataForSEO returns task-level error dicts)
    # ------------------------------------------------------------
    @staticmethod
    def _extract_cost(data: dict) -> float | None:
        """Best-effort cost/credits from the response payload."""
        try:
            task = data["tasks"][0]
            cost = task.get("credits")
            if cost is None:
                return None
            return float(cost)
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    def parse(self, raw: dict) -> dict:
        """Parse the full response body into a structured result dict.

        Returns ``{"organic": [...], "paa": [...], "related": [...],
        "featured_snippet": {...} | None}``. Raises ``PipelineError`` on
        transport/task-level failures.
        """
        if raw.get("status_code") != 200:
            raise PipelineError(
                ErrorCode.DATAFORSEO_REQUEST_FAILED,
                f"DataForSEO status_code={raw.get('status_code')} "
                f"message={raw.get('message')!r}",
                raw=raw,
            )
        tasks = raw.get("tasks") or []
        if not tasks:
            raise PipelineError(
                ErrorCode.DATAFORSEO_EMPTY_SERP,
                "DataForSEO response contains no tasks",
                raw=raw,
            )
        task = tasks[0]
        result = task.get("result")
        # Task-level error: result is a dict with code/message, not data.
        if not isinstance(result, dict):
            raise PipelineError(
                ErrorCode.DATAFORSEO_REQUEST_FAILED,
                f"DataForSEO task has no result (code={task.get('code')}, "
                f"message={task.get('message')!r})",
                raw=task,
            )
        if result.get("code") not in (200, None) or not (
            result.get("organic") or result.get("people_also_ask")
            or result.get("related_searches")
        ):
            raise PipelineError(
                ErrorCode.DATAFORSEO_EMPTY_SERP,
                f"DataForSEO task returned no SERP data "
                f"(code={result.get('code')}, message={result.get('message')!r})",
                raw=result,
            )
        return {
            "organic": result.get("organic") or [],
            "paa": result.get("people_also_ask") or [],
            "related": result.get("related_searches") or [],
            "featured_snippet": result.get("featured_snippet"),
        }

    # ------------------------------------------------------------
    # SERPProvider interface
    # ------------------------------------------------------------
    async def search(self, request: SERPRequest) -> SERPResponse:
        raw, cost = await self._request_raw(request)
        parsed = self.parse(raw)

        organic = [
            OrganicResult(
                rank=int(item.get("position") or 0),
                title=item.get("title") or "",
                url=item.get("url") or "",
                domain=item.get("domain"),
                snippet=item.get("description"),
            )
            for item in parsed["organic"]
            if isinstance(item, dict) and item.get("url")
        ]
        organic.sort(key=lambda r: r.rank)

        paa: list[PAAQuestion] = []
        for block in parsed["paa"]:
            if not isinstance(block, dict):
                continue
            for q in block.get("questions") or []:
                if isinstance(q, dict) and q.get("question"):
                    paa.append(
                        PAAQuestion(
                            question=q["question"],
                            source_url=q.get("url"),
                        )
                    )

        related = [
            item["title"]
            for item in parsed["related"]
            if isinstance(item, dict) and item.get("title")
        ]

        if not organic:
            raise PipelineError(
                ErrorCode.DATAFORSEO_EMPTY_SERP,
                "DataForSEO response contained no organic results",
                raw=raw,
            )

        return SERPResponse(
            keyword=request.keyword,
            organic_results=organic,
            paa_questions=paa,
            related_searches=related,
            raw=raw,
            provider_cost=cost,
        )

    async def health_check(self) -> bool:
        """Configured credentials + endpoint reachable.

        A DataForSEO "auth failed" error page (HTTP 200 with a task-level
        error, or HTTP 401) reports False.
        """
        if not self._settings.dataforseo_configured:
            return False
        try:
            await self._request_raw(
                SERPRequest(
                    keyword="health check",
                    location_code=self._settings.dataforseo_location_code,
                    language_code=self._settings.dataforseo_language_code,
                    device="desktop",
                    depth=1,
                )
            )
            return True
        except PipelineError as exc:
            logger.info(
                "dataforseo_health_check_failed",
                extra={
                    "event": "dataforseo_health_check_failed",
                    "error_code": exc.error_code.value,
                },
            )
            return False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
