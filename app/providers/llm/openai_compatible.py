"""OpenAI-compatible LLM provider (SEO-AUTO-DEV-SPEC.md sections 10.1, 49, 51).

Talks to any OpenAI-compatible ``/chat/completions`` endpoint (V1
default: local llama.cpp + Qwen3.8-27B). No model-specific SDK is used;
only ``httpx``.

Retry policy (spec section 51):
    retryable      429, 500, 502, 503, 504, timeout, connection reset
    non-retryable  400, 401, 403, 404 (configuration errors)
    backoff        2s -> 5s -> 15s, max 3 attempts (LLM: 2 retries)

Structured output (spec section 49):
    LLM call -> extract JSON -> json.loads -> Pydantic validation
    -> on failure: JSON repair prompt, at most 2 repairs
    -> still failing: LLM_STRUCTURED_OUTPUT_INVALID (raw output kept)
"""

import asyncio
import json
import logging
import time
from typing import Type, TypeVar

import httpx
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.core.exceptions import ErrorCode, PipelineError
from app.providers.llm.base import LLMProvider

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)

#: Spec section 51: backoff 2s / 5s / 15s, max 3 attempts.
RETRY_BACKOFF_SECONDS = (2.0, 5.0, 15.0)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
NON_RETRYABLE_STATUS = {400, 401, 403, 404}

#: Spec section 49: at most 2 repair retries for structured output.
MAX_STRUCTURED_REPAIRS = 2


class OpenAICompatibleLLMProvider(LLMProvider):
    """LLMProvider backed by an OpenAI-compatible chat completions API."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        backoff_seconds: tuple[float, ...] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        # Injectable backoff sequence for tests (production: spec 2s/5s/15s).
        self._backoff_seconds = backoff_seconds or RETRY_BACKOFF_SECONDS
        # ``client`` injection is for testing (httpx.MockTransport).
        self._client = client or httpx.AsyncClient(
            base_url=self._settings.llm_base_url,
            headers={"Authorization": f"Bearer {self._settings.llm_api_key}"},
            timeout=httpx.Timeout(self._settings.llm_timeout_seconds),
        )
        self._owns_client = client is None
        # Per-logical-call token accounting for cost metering (P9-B1, spec
        # section 54): ``_chat`` accumulates the ``usage`` block of every
        # HTTP response of the current logical call (a
        # ``generate_structured`` with its repair attempts counts once).
        # The meter resets it before each logical call and reads it back
        # afterwards; ``None`` means the endpoint reported no usage.
        self._usage: tuple[int, int] | None = None

    # ------------------------------------------------------------
    # Low-level chat call with the unified retry policy
    # ------------------------------------------------------------
    async def _chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float | None,
        max_tokens: int | None = None,
    ) -> str:
        """One chat completion with HTTP retry (spec section 51)."""
        temperature = (
            self._settings.llm_temperature_analysis
            if temperature is None
            else temperature
        )
        payload: dict = {
            "model": self._settings.llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        attempts = self._settings.llm_max_retries + 1
        last_error: Exception | None = None
        started = time.monotonic()

        for attempt in range(attempts):
            try:
                response = await self._client.post("/chat/completions", json=payload)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                # Timeout / connection reset -> retryable.
                last_error = exc
                logger.warning(
                    "llm_request_failed",
                    extra={
                        "event": "llm_request_failed",
                        "provider": self._settings.llm_provider,
                        "error_code": "TIMEOUT_OR_NETWORK",
                        "attempt": attempt + 1,
                    },
                )
                await self._backoff(attempt)
                continue

            if response.status_code == 200:
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                self._accumulate_usage(data)
                logger.info(
                    "llm_request_ok",
                    extra={
                        "event": "llm_request_ok",
                        "provider": self._settings.llm_provider,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "attempts": attempt + 1,
                    },
                )
                return content

            if response.status_code in NON_RETRYABLE_STATUS:
                raise PipelineError(
                    ErrorCode.LLM_UNAVAILABLE,
                    f"LLM endpoint returned {response.status_code}: "
                    f"{response.text[:500]}",
                    raw=response.text[:5000],
                )

            # 429 / 5xx -> retryable.
            last_error = PipelineError(
                ErrorCode.LLM_UNAVAILABLE,
                f"LLM endpoint returned {response.status_code}",
                raw=response.text[:5000],
            )
            logger.warning(
                "llm_request_failed",
                extra={
                    "event": "llm_request_failed",
                    "provider": self._settings.llm_provider,
                    "error_code": f"HTTP_{response.status_code}",
                    "attempt": attempt + 1,
                },
            )
            await self._backoff(attempt)

        raise PipelineError(
            ErrorCode.LLM_UNAVAILABLE,
            f"LLM request failed after {attempts} attempts",
            raw=str(last_error) if last_error else None,
        )

    def _accumulate_usage(self, data: dict) -> None:
        """Fold the response ``usage`` block into the logical-call total."""
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if not isinstance(prompt_tokens, (int, float)) or not isinstance(
            completion_tokens, (int, float)
        ):
            return
        base = self._usage or (0, 0)
        self._usage = (base[0] + int(prompt_tokens), base[1] + int(completion_tokens))

    def begin_usage(self) -> None:
        """Reset the accumulator before a new logical LLM call."""
        self._usage = None

    def take_usage(self) -> tuple[int, int] | None:
        """Return the accumulated tokens of the last logical call."""
        return self._usage

    async def _backoff(self, attempt: int) -> None:
        if attempt < len(self._backoff_seconds):
            await asyncio.sleep(self._backoff_seconds[attempt])

    # ------------------------------------------------------------
    # LLMProvider interface
    # ------------------------------------------------------------
    async def generate_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        return await self._chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: Type[T],
        temperature: float | None = None,
    ) -> T:
        """Structured output with JSON repair (spec section 49)."""
        schema = json.dumps(response_model.model_json_schema(), ensure_ascii=False)
        base_system = (
            system_prompt
            + "\n\nRespond with ONLY a single valid JSON object matching this "
            "JSON schema. No markdown fences, no commentary:\n"
            + schema
        )

        last_output = ""
        last_error: Exception | None = None

        for repair in range(MAX_STRUCTURED_REPAIRS + 1):
            if repair == 0:
                prompt = user_prompt
            else:
                prompt = (
                    user_prompt
                    + "\n\nYour previous response was invalid JSON or failed "
                    "schema validation.\n\nPrevious response:\n"
                    + last_output[:8000]
                    + "\n\nValidation error:\n"
                    + str(last_error)
                    + "\n\nReturn ONLY the corrected JSON object."
                )

            output = await self._chat(
                system_prompt=base_system,
                user_prompt=prompt,
                temperature=temperature,
            )
            last_output = output
            try:
                data = _extract_json_object(output)
                return response_model.model_validate(data)
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                logger.warning(
                    "llm_structured_output_invalid",
                    extra={
                        "event": "llm_structured_output_invalid",
                        "provider": self._settings.llm_provider,
                        "repair_attempt": repair,
                        "error_code": "RETRYING",
                    },
                )

        raise PipelineError(
            ErrorCode.LLM_STRUCTURED_OUTPUT_INVALID,
            f"structured output failed after {MAX_STRUCTURED_REPAIRS} repairs: "
            f"{last_error}",
            raw=last_output,
        )

    async def health_check(self) -> bool:
        """Reachable + models endpoint answers (spec section 57)."""
        try:
            response = await self._client.get("/models")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


# ----------------------------------------------------------------------
# JSON extraction helpers
# ----------------------------------------------------------------------
def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        # remove leading fence (optionally ```json) and trailing fence
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[: -3]
    return text.strip()


def _extract_json_object(text: str) -> dict:
    """Extract the first top-level JSON object from raw LLM output.

    Tolerates leading/trailing commentary and code fences. Raises
    ``ValueError`` when no balanced object is found or it is not valid
    JSON.
    """
    text = _strip_code_fences(text)

    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in LLM output")

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                data = json.loads(candidate)
                if not isinstance(data, dict):
                    raise ValueError("top-level JSON value is not an object")
                return data

    raise ValueError("unbalanced JSON object in LLM output")
