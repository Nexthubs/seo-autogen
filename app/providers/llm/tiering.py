"""Two-tier LLM provider: separate endpoints for writing vs analysis.

Follow-up to docs/TASK-LLM-MODEL-TIERING.md. The analysis tier can live on a
different LLM vendor/endpoint than the writing tier (e.g. writing on a
local/Gemini box, analysis on an OpenAI-compatible one) by setting the
``LLM_BASE_URL_ANALYSIS`` / ``LLM_API_KEY_ANALYSIS`` / ``LLM_MODEL_ANALYSIS``
variables — each independently, with empty falling back to the writing-tier
value.

Routing contract (the pipeline steps rely on it):

- ``model=None``            → writing tier (``LLM_MODEL`` / default endpoint);
- ``model=<explicit name>`` → analysis tier. When the analysis tier is
  configured with a *distinct* endpoint triple the call goes to a second
  ``OpenAICompatibleLLMProvider`` built on the analysis endpoint; otherwise
  both tiers share one provider and only the model name differs (the
  single-endpoint behaviour from the original tiering task).

Only ``generate_structured`` carries the ``model`` parameter (the frozen
interface), so routing happens there; ``generate_text`` always uses the
writing tier.

Provenance: the meter / ``llm_model_name`` read ``_last_model`` off the
provider — this wrapper proxies it from whichever instance actually served
the last logical call, and ``begin_usage``/``take_usage`` reset/read the
per-instance accumulators so ``llm_usage`` rows always describe the call
that really happened.
"""

from __future__ import annotations

from typing import Type, TypeVar

import httpx
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.providers.llm.base import LLMProvider
from app.providers.llm.openai_compatible import OpenAICompatibleLLMProvider

T = TypeVar("T", bound=BaseModel)


class TieredLLMProvider(LLMProvider):
    """LLMProvider dispatching the writing / analysis tiers to (at most) two
    OpenAI-compatible endpoints."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        writing_client: httpx.AsyncClient | None = None,
        analysis_client: httpx.AsyncClient | None = None,
        backoff_seconds: tuple[float, ...] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._writing = OpenAICompatibleLLMProvider(
            settings=self._settings,
            client=writing_client,
            backoff_seconds=backoff_seconds,
        )
        # A second connection only when the analysis tier is configured with
        # a distinct endpoint triple; otherwise every call (both tiers) goes
        # to the writing-tier provider, exactly like the pre-endpoint-split
        # single-provider behaviour.
        self._analysis: OpenAICompatibleLLMProvider | None = None
        if self._settings.llm_analysis_provider_distinct:
            analysis_settings = self._settings.model_copy(
                update={
                    "llm_base_url": self._settings.llm_analysis_base_url,
                    "llm_api_key": self._settings.llm_analysis_api_key,
                    "llm_model": self._settings.llm_analysis_model
                    or self._settings.llm_model,
                    "llm_structured_output_mode": (
                        self._settings.llm_analysis_structured_output_mode
                    ),
                    "llm_structured_output_mode_analysis": "",
                }
            )
            self._analysis = OpenAICompatibleLLMProvider(
                settings=analysis_settings,
                client=analysis_client,
                backoff_seconds=backoff_seconds,
            )
        #: The instance that served the last logical call (provenance proxy).
        self._last: OpenAICompatibleLLMProvider | None = None

    # ------------------------------------------------------------
    # Provenance / metering surface (read by MeteredLLMProvider and
    # steps._common.llm_model_name — they unwrap only the meter).
    # ------------------------------------------------------------
    @property
    def _last_model(self) -> str | None:
        target = self._last or self._writing
        return target._last_model

    def _pick(self, model: str | None) -> OpenAICompatibleLLMProvider:
        """Route: ``None`` → writing tier; explicit model → analysis tier
        (falling back to the shared writing provider when no distinct
        analysis endpoint is configured)."""
        if model is None:
            return self._writing
        if self._analysis is not None:
            return self._analysis
        return self._writing

    def begin_usage(self) -> None:
        # Reset every instance's accumulator (no I/O); ``take_usage`` reads
        # back only the instance that served the call.
        self._writing.begin_usage()
        if self._analysis is not None:
            self._analysis.begin_usage()
        self._last = None

    def take_usage(self) -> tuple[int, int] | None:
        target = self._last or self._writing
        return target.take_usage()

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
        # The frozen interface has no ``model`` parameter: free-form text is
        # always a writing-tier call.
        self._last = self._writing
        return await self._writing.generate_text(
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
        model: str | None = None,
    ) -> T:
        target = self._pick(model)
        self._last = target
        return await target.generate_structured(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=response_model,
            temperature=temperature,
            model=model,
        )

    async def health_check(self) -> bool:
        # Reachability of EVERY configured tier endpoint; a single shared
        # endpoint is checked exactly once.
        ok = await self._writing.health_check()
        if self._analysis is not None:
            ok = ok and await self._analysis.health_check()
        return ok

    async def aclose(self) -> None:
        await self._writing.aclose()
        if self._analysis is not None:
            await self._analysis.aclose()
