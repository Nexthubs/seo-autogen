"""Shared helpers for pipeline steps."""

from app.providers.llm.base import LLMProvider


def llm_model_name(llm: LLMProvider) -> str | None:
    """Best-effort model name for provenance columns (46.9-46.12)."""
    settings = getattr(llm, "_settings", None)
    return getattr(settings, "llm_model", None)
