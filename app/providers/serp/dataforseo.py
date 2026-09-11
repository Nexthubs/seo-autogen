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

Response contract (official, machine-verified against the documented
example; see ``tests/unit/test_dataforseo_provider.py``):

    {
      "status_code": 20000,            # business code (HTTP is always 200)
      "status_message": "OK",
      "cost": 0.003,                   # USD cost of the whole request
      "tasks": [
        {
          "status_code": 20000,        # task-level business code
          "cost": 0.003,               # USD cost of this task
          "result": [                  # a LIST of SERP blocks
            {
              "type": "organic",
              "items": [               # flat, MIXED-type item list
                {"type": "organic", "rank_group": 26, "rank_absolute": 30,
                 "position": "left", "domain": "...", "title": "...",
                 "url": "...", "description": "..."},
                {"type": "featured_snippet", "title": "...", "url": "...",
                 "description": "...", "domain": "..."},
                {"type": "people_also_ask",
                 "items": [{"type": "people_also_ask_element",
                            "title": "<question>",
                            "expanded_element": [
                              {"type": "people_also_ask_expanded_element",
                               "url": "...", "title": "...", "description": "..."}]}]},
                {"type": "related_searches",
                 "items": ["iphone xr", "iphone xs", ...]},
                ...
              ],
            }
          ],
        }
      ],
    }

The parser therefore scans each block's ``items[]`` and dispatches every
item by its own ``type`` — it does NOT assume a single ``result["organic"]``
dict. Organic rank comes from ``rank_group`` (the ``position`` field is a
left/right string, not a rank). A task is a failure when its
``status_code != 20000`` or its ``result`` is not a non-empty list.

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
    FeaturedSnippet,
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
                try:
                    data = response.json()
                except ValueError as exc:
                    raise PipelineError(
                        ErrorCode.DATAFORSEO_REQUEST_FAILED,
                        "DataForSEO returned invalid JSON",
                        raw=response.text[:5000],
                        provider_cost_reported=True,
                    ) from exc
                if not isinstance(data, dict):
                    raise PipelineError(
                        ErrorCode.DATAFORSEO_REQUEST_FAILED,
                        "DataForSEO returned a non-object response",
                        raw=data,
                        provider_cost_reported=True,
                    )
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
        """Best-effort USD cost from the response payload.

        DataForSEO reports cost in US dollars under the ``cost`` key (never
        ``credits``). The per-task ``cost`` is authoritative; the top-level
        ``cost`` (the cost of the whole request) is the fallback when the
        task does not carry one. Returns ``None`` when neither is present or
        numeric.
        """
        try:
            task = data["tasks"][0]
        except (KeyError, IndexError, TypeError):
            task = None
        candidates: list[object] = []
        if isinstance(task, dict):
            candidates.append(task.get("cost"))
        candidates.append(data.get("cost"))
        for value in candidates:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        return None

    def parse(self, raw: dict) -> dict:
        """Parse the full response body into a structured result dict.

        Returns ``{"organic": [...], "paa": [...], "related": [...],
        "featured_snippet": dict | None}`` where the three lists hold the
        *item* dicts (one per SERP element) and ``featured_snippet`` is the
        raw featured-snippet item (or ``None``). Raises ``PipelineError`` on
        transport/task-level failures.

        The official contract (see module docstring) is:
          - top-level ``status_code == 20000`` (HTTP is always 200);
          - ``tasks[0].status_code == 20000`` and ``tasks[0].result`` is a
            LIST of SERP blocks (a task failure has ``result`` null or a
            non-list);
          - each block's ``items[]`` is a flat, mixed-type list, so every
            item is dispatched by its own ``type``.
        """
        if raw.get("status_code") != 20000:
            raise PipelineError(
                ErrorCode.DATAFORSEO_REQUEST_FAILED,
                f"DataForSEO status_code={raw.get('status_code')} "
                f"status_message={raw.get('status_message')!r}",
                # R-H04: structured payloads are allowed at the provider
                # boundary; PipelineError normalizes them into one redacted
                # JSON string (never a dict) for every consumer.
                raw=raw,
            )
        tasks = raw.get("tasks") or []
        if not tasks:
            raise PipelineError(
                ErrorCode.DATAFORSEO_REQUEST_FAILED,
                "DataForSEO response contains no tasks",
                raw=raw,
            )
        task = tasks[0]
        if not isinstance(task, dict) or task.get("status_code") != 20000:
            raise PipelineError(
                ErrorCode.DATAFORSEO_REQUEST_FAILED,
                f"DataForSEO task failed (status_code={task.get('status_code')}, "
                f"status_message={task.get('status_message')!r})",
                raw=task,
            )
        result = task.get("result")
        # A task failure leaves `result` null (or a non-list error payload).
        if not isinstance(result, list) or not result:
            raise PipelineError(
                ErrorCode.DATAFORSEO_REQUEST_FAILED,
                f"DataForSEO task has no result "
                f"(status_code={task.get('status_code')}, "
                f"status_message={task.get('status_message')!r})",
                raw=task,
            )

        organic: list[dict] = []
        paa: list[dict] = []
        related: list[str] = []
        featured: dict | None = None
        for block in result:
            if not isinstance(block, dict):
                continue
            for item in block.get("items") or []:
                if not isinstance(item, dict):
                    continue
                itype = item.get("type")
                if itype == "organic":
                    if item.get("url"):
                        organic.append(item)
                elif itype == "people_also_ask":
                    paa.append(item)
                elif itype == "related_searches":
                    for rel in item.get("items") or []:
                        if isinstance(rel, str) and rel:
                            related.append(rel)
                elif itype == "featured_snippet":
                    if featured is None and item.get("url"):
                        featured = item
        return {
            "organic": organic,
            "paa": paa,
            "related": related,
            "featured_snippet": featured,
        }

    # ------------------------------------------------------------
    # SERPProvider interface
    # ------------------------------------------------------------
    async def search(self, request: SERPRequest) -> SERPResponse:
        raw, cost = await self._request_raw(request)
        try:
            parsed = self.parse(raw)
        except PipelineError as exc:
            # R3-M02: a successful HTTP response may carry a confirmed cost
            # even when its business payload is unusable. Preserve the
            # telemetry on the failure boundary for the step-level ledger.
            exc.provider_cost = cost
            exc.provider_cost_reported = True
            raise

        # `organic` already holds only item dicts with a url (parse filtered
        # them). Rank comes from `rank_group`; the `position` field is a
        # left/right string and is deliberately ignored.
        organic = [
            OrganicResult(
                rank=int(item.get("rank_group") or 0),
                title=item.get("title") or "",
                url=item.get("url") or "",
                domain=item.get("domain"),
                snippet=item.get("description"),
            )
            for item in parsed["organic"]
        ]
        organic.sort(key=lambda r: r.rank)

        # A `people_also_ask` item's `items[]` is a list of
        # `people_also_ask_element` dicts; the question is the element
        # `title`, the source URL the first `expanded_element` entry's
        # `url` (defensive: some element variants carry no url).
        paa: list[PAAQuestion] = []
        for item in parsed["paa"]:
            for q in item.get("items") or []:
                if not isinstance(q, dict) or q.get("type") != "people_also_ask_element":
                    continue
                question = q.get("title")
                if not question:
                    continue
                source_url = None
                for exp in q.get("expanded_element") or []:
                    if isinstance(exp, dict) and exp.get("url"):
                        source_url = exp["url"]
                        break
                paa.append(
                    PAAQuestion(question=question, source_url=source_url)
                )

        related = parsed["related"]
        featured = parsed["featured_snippet"]

        if not organic:
            raise PipelineError(
                ErrorCode.DATAFORSEO_EMPTY_SERP,
                "DataForSEO response contained no organic results",
                raw=raw,
                provider_cost=cost,
                provider_cost_reported=True,
            )

        return SERPResponse(
            keyword=request.keyword,
            organic_results=organic,
            paa_questions=paa,
            related_searches=related,
            featured_snippet=(
                FeaturedSnippet(
                    title=featured.get("title"),
                    snippet=featured.get("description"),
                    url=featured.get("url"),
                    domain=featured.get("domain"),
                )
                if featured
                else None
            ),
            raw=raw,
            provider_cost=cost,
        )

    async def health_check(self) -> bool:
        """Configured credentials + a successful live SERP call.

        DataForSEO always answers HTTP 200 and carries the real outcome in
        the body's ``status_code`` (20000 = ok), so an HTTP-only check would
        treat an auth/business failure (e.g. status_code 40101, or a task
        error) as "connected". This probe therefore runs a real (paid,
        ~one SERP call) live search with ``depth=1`` and requires the
        business envelope to parse successfully: top-level AND task-level
        ``status_code == 20000`` with a non-empty ``result`` list. Any
        ``PipelineError`` (HTTP 401/403, business failure, empty SERP) is
        False.
        """
        if not self._settings.dataforseo_configured:
            return False
        try:
            raw, _ = await self._request_raw(
                SERPRequest(
                    keyword="health check",
                    location_code=self._settings.dataforseo_location_code,
                    language_code=self._settings.dataforseo_language_code,
                    device="desktop",
                    depth=1,
                )
            )
            self.parse(raw)
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
