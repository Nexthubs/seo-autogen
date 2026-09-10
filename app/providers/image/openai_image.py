"""OpenAI-compatible image provider
(SEO-AUTO-DEV-SPEC.md sections 2.4, 10.4, 34, 51).

Talks to any OpenAI-compatible ``POST /images/generations`` endpoint
(no model-specific SDK, only ``httpx``). The V1 model is
``gpt-image-2``; the endpoint, key and model all come from settings.

Retry policy (spec section 51, Image: 2 retry):
    retryable      429, 500, 502, 503, 504, timeout, connection reset
    non-retryable  400, 401, 403, 404 (configuration errors)
    backoff        2s -> 5s -> 15s (max 3 attempts)

Responses may carry the image as ``b64_json`` or as a ``url``
(downloaded via the same client).
"""

import asyncio
import base64
import binascii
import logging
import time

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import ErrorCode, PipelineError
from app.providers.image.base import ImageProvider
from app.schemas.images import GeneratedImage, ImageGenerationRequest
from app.services.image_storage import save_image_bytes

logger = logging.getLogger(__name__)

#: Spec section 51: backoff 2s / 5s / 15s, max 3 attempts (Image: 2 retry).
RETRY_BACKOFF_SECONDS = (2.0, 5.0, 15.0)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

#: aspect_ratio -> Images API ``size`` (1024-class resolutions).
_SIZE_BY_RATIO = {
    "16:9": "1536x1024",
    "9:16": "1024x1536",
    "1:1": "1024x1024",
    "4:3": "1152x864",
    "3:4": "864x1152",
}
_DEFAULT_SIZE = "1536x1024"

def _extract_cost(data: dict) -> float | None:
    """Best-effort cost from the image API response (spec section 54).

    Most OpenAI-compatible Images APIs report no per-request cost, so the
    result is usually None. The field is probed defensively: any shape
    mismatch (missing key, non-numeric) yields None instead of an error.
    """
    cost = data.get("cost")
    if cost is None:
        # Some endpoints nest it under ``usage``.
        usage = data.get("usage")
        if isinstance(usage, dict):
            cost = usage.get("cost")
    if cost is None:
        return None
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        return float(cost)
    return None


#: Minimal PNG signature — sanity check for decoded bytes.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8"
_WEBP_MAGIC = b"RIFF"


class OpenAIImageProvider(ImageProvider):
    """ImageProvider backed by an OpenAI-compatible Images API."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        backoff_seconds: tuple[float, ...] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._backoff_seconds = backoff_seconds or RETRY_BACKOFF_SECONDS
        self._client = client or httpx.AsyncClient(
            base_url=self._settings.image_base_url,
            timeout=httpx.Timeout(self._settings.image_timeout_seconds),
        )
        self._owns_client = client is None
        self._auth = f"Bearer {self._settings.image_api_key}"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------
    # ImageProvider interface
    # ------------------------------------------------------------
    async def generate(self, request: ImageGenerationRequest) -> GeneratedImage:
        """Generate one image and store it locally (section 10.4)."""
        payload: dict = {
            "model": self._settings.image_model,
            "prompt": request.prompt,
            "size": _SIZE_BY_RATIO.get(request.aspect_ratio, _DEFAULT_SIZE),
            "n": 1,
            "quality": self._settings.image_quality,
        }

        data = await self._post_with_retry("/images/generations", payload)
        item = (data.get("data") or [None])[0] or {}
        b64 = item.get("b64_json")
        if b64:
            try:
                image_bytes = base64.b64decode(b64)
            except (binascii.Error, ValueError) as exc:
                raise PipelineError(
                    ErrorCode.IMAGE_PROVIDER_FAILED,
                    f"invalid b64_json payload: {exc}",
                    raw=str(b64)[:200],
                ) from exc
        elif item.get("url"):
            image_bytes = await self._download(item["url"])
        else:
            raise PipelineError(
                ErrorCode.IMAGE_PROVIDER_FAILED,
                "image response contained neither b64_json nor url",
            )

        if not _looks_like_image(image_bytes):
            raise PipelineError(
                ErrorCode.IMAGE_PROVIDER_FAILED,
                "decoded payload is not a recognizable image",
            )

        if not request.filename:
            raise PipelineError(
                ErrorCode.IMAGE_PROVIDER_FAILED,
                "request.filename is required for local storage",
            )
        local_path = save_image_bytes(
            request.job_id or "nojob",
            image_bytes,
            request.filename,
            settings=self._settings,
        )
        return GeneratedImage(
            local_path=str(local_path),
            filename=request.filename,
            mime_type=_guess_mime(image_bytes),
            prompt=request.prompt,
            provider=self._settings.image_model,
            provider_request_id=data.get("id"),
            provider_cost=_extract_cost(data),
        )

    async def health_check(self) -> bool:
        """True when the endpoint answers AND serves the configured model."""
        try:
            response = await self._client.get(
                "/models", headers={"Authorization": self._auth}
            )
            if response.status_code != 200:
                return False
            data = response.json()
            ids = {m.get("id") for m in data.get("data", []) if isinstance(m, dict)}
            return self._settings.image_model in ids
        except (httpx.HTTPError, ValueError):
            return False

    # ------------------------------------------------------------
    # internals
    # ------------------------------------------------------------
    async def _post_with_retry(
        self, path: str, payload: dict
    ) -> dict:
        attempts = 3
        last_error: Exception | None = None
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(self._backoff_seconds[min(attempt, 2)])
            try:
                response = await self._client.post(
                    path, json=payload, headers={"Authorization": self._auth}
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                logger.warning(
                    "image_request_failed",
                    extra={
                        "event": "image_request_failed",
                        "path": path,
                        "attempt": attempt + 1,
                        "error": str(exc),
                    },
                )
                continue
            if response.status_code in RETRYABLE_STATUS:
                last_error = PipelineError(
                    ErrorCode.IMAGE_PROVIDER_FAILED,
                    f"retryable HTTP {response.status_code}",
                )
                continue
            if response.status_code != 200:
                raise PipelineError(
                    ErrorCode.IMAGE_PROVIDER_FAILED,
                    f"images API HTTP {response.status_code}: "
                    f"{response.text[:300]}",
                )
            try:
                return response.json()
            except ValueError as exc:
                raise PipelineError(
                    ErrorCode.IMAGE_PROVIDER_FAILED,
                    "images API returned non-JSON body",
                    raw=response.text[:300],
                ) from exc
        raise PipelineError(
            ErrorCode.IMAGE_PROVIDER_FAILED,
            f"images API failed after {attempts} attempts",
            raw=str(last_error),
        )

    async def _download(self, url: str) -> bytes:
        response = await self._client.get(url, headers={"Authorization": self._auth})
        if response.status_code != 200:
            raise PipelineError(
                ErrorCode.IMAGE_PROVIDER_FAILED,
                f"image download HTTP {response.status_code}",
            )
        return response.content


def _looks_like_image(data: bytes) -> bool:
    return (
        data.startswith(_PNG_MAGIC)
        or data[:2] == _JPEG_MAGIC
        or data[:4] == _WEBP_MAGIC
    )


def _guess_mime(data: bytes) -> str:
    if data.startswith(_PNG_MAGIC):
        return "image/png"
    if data[:2] == _JPEG_MAGIC:
        return "image/jpeg"
    if data[:4] == _WEBP_MAGIC:
        return "image/webp"
    return "application/octet-stream"
