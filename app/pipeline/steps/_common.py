"""Shared helpers for pipeline steps."""

from app.providers.llm.base import LLMProvider
from app.services.prompt_service import PromptSpec


def llm_model_name(llm: LLMProvider) -> str | None:
    """Best-effort model name for provenance columns (46.9-46.12).

    Tier-aware (docs/TASK-LLM-MODEL-TIERING.md): when the last logical
    call used a per-call analysis-tier model, the underlying
    ``OpenAICompatibleLLMProvider`` records it in ``_last_model``; that
    wins over the configured default so an analysis/review row is never
    mislabelled with the writing-tier model name.
    """
    inner = getattr(llm, "_inner", None)  # unwrap MeteredLLMProvider
    last = getattr(inner, "_last_model", None)
    if isinstance(last, str) and last:
        return last
    settings = getattr(inner or llm, "_settings", None)
    return getattr(settings, "llm_model", None)


def set_llm_prompt(llm: LLMProvider, prompt: PromptSpec | None) -> None:
    """Record the prompt of the call about to be made on the meter.

    Spec section 48 requires ``prompt_name`` / ``prompt_version`` /
    ``prompt_hash`` on every LLM usage row. The meter
    (``MeteredLLMProvider``) reads ``current_prompt`` when it records the
    row after each logical call, so steps set it right before invoking the
    LLM. A bare provider without metering (tests) simply has no
    ``current_prompt`` attribute and this is a no-op.
    """
    if prompt is None:
        return
    meter = llm
    if not hasattr(meter, "current_prompt"):
        return
    meter.current_prompt = (prompt.name, prompt.version, prompt.prompt_hash)
