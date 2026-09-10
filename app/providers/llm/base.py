"""LLMProvider interface (SEO-AUTO-DEV-SPEC.md section 10.1)."""

import abc
from typing import Type, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMProvider(abc.ABC):
    """Model-agnostic LLM access.

    V1 implementation: ``OpenAICompatibleLLMProvider``. The pipeline
    must not depend on any model-specific SDK.
    """

    @abc.abstractmethod
    async def generate_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Free-form text completion."""

    @abc.abstractmethod
    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: Type[T],
        temperature: float | None = None,
    ) -> T:
        """Structured output validated against a Pydantic model.

        Implementations must apply the structured-output fault tolerance
        of spec section 49 (JSON extraction, validation, at most 2
        repair retries, raw output preserved on failure).
        """

    @abc.abstractmethod
    async def health_check(self) -> bool:
        """Return True when the LLM endpoint is reachable."""
