"""LLM usage metering wrapper (P9-B1 — spec section 54).

The V1 LLM is a local model with no per-call bill, so cost tracking for it
means recording the *tokens* and *wall duration* of every logical LLM call.

``MeteredLLMProvider`` decorates any :class:`LLMProvider`. It:

- tracks the current pipeline step name (set by the orchestrator at each
  step boundary, before the step's runners execute);
- measures the wall duration of each logical call (a
  ``generate_structured`` with its JSON-repair attempts counts as ONE call);
- reads the token counts the underlying provider accumulated (the
  ``OpenAICompatibleLLMProvider`` exposes ``begin_usage``/``take_usage``);
- appends one :class:`~app.db.models.llm_usage.LLMUsageRow` to the
  pipeline session. It does NOT commit: the owning step's own
  ``session.commit()`` persists the row, and a step that fails before
  committing leaves the row pending so it rides the orchestrator's failure
  commit. That is exactly what we want — the usage always reflects the calls
  actually made for the currently committed step outputs.

Retrying a step deletes its rows via ``checkpoints.reset_from_step`` (see
there), so the meter never double-counts a re-run.
"""

from __future__ import annotations

import time
from typing import Type, TypeVar

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.models.llm_usage import LLMUsageRow
from app.providers.llm.base import LLMProvider

T = TypeVar("T", bound=BaseModel)


def _model_name(inner: LLMProvider) -> str | None:
    """Best-effort model name from the underlying provider's settings."""
    settings = getattr(inner, "_settings", None)
    model = getattr(settings, "llm_model", None)
    return model if isinstance(model, str) and model else None


class MeteredLLMProvider(LLMProvider):
    """LLMProvider that records one ``llm_usage`` row per logical call."""

    def __init__(
        self,
        inner: LLMProvider,
        session: Session,
        job_id: object,
    ) -> None:
        self._inner = inner
        self._session = session
        self._job_id = job_id
        #: Set by the orchestrator before each step's runners execute.
        self.current_step: str = ""
        self._model = _model_name(inner)

    def _record(self, duration_ms: int, usage: tuple[int, int] | None) -> None:
        row = LLMUsageRow(
            job_id=self._job_id,
            step=self.current_step,
            model=self._model,
            input_tokens=usage[0] if usage else None,
            output_tokens=usage[1] if usage else None,
            duration_ms=duration_ms,
        )
        self._session.add(row)

    def _begin(self) -> None:
        begin = getattr(self._inner, "begin_usage", None)
        if callable(begin):
            begin()

    def _usage(self) -> tuple[int, int] | None:
        take = getattr(self._inner, "take_usage", None)
        return take() if callable(take) else None

    async def generate_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        self._begin()
        started = time.monotonic()
        try:
            return await self._inner.generate_text(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            self._record(duration_ms, self._usage())

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: Type[T],
        temperature: float | None = None,
    ) -> T:
        self._begin()
        started = time.monotonic()
        try:
            return await self._inner.generate_structured(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_model=response_model,
                temperature=temperature,
            )
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            self._record(duration_ms, self._usage())

    async def health_check(self) -> bool:
        return await self._inner.health_check()

    async def aclose(self) -> None:
        aclose = getattr(self._inner, "aclose", None)
        if aclose is None:
            return
        await aclose()
