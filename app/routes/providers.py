"""Provider status route (spec 44: ``GET /api/providers/status``, 57, 60).

Returns reachability booleans ONLY — section 60 forbids any secret (key,
token, URL) from appearing in the UI. The check is best-effort and tolerant:
an unconfigured provider simply reports ``reachable: false``.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter

from app.core.config import get_settings
from app.workers.article_tasks import build_providers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/providers", tags=["providers"])

_ORDER = (
    ("llm", "llm"),
    ("serp", "serp"),
    ("extractor", "extractor"),
    ("image", "image"),
    ("cms", "cms"),
)


@router.get("/status")
def api_provider_status() -> dict:
    settings = get_settings()
    providers = build_providers(settings)
    results = asyncio.run(_check_all(providers))
    providers_out = {
        name: results[name]
        for name, _ in _ORDER
    }
    # Model tiering (docs/TASK-LLM-MODEL-TIERING.md): model names are not
    # secrets (spec 60 restricts keys/tokens/URLs), so the effective
    # writing / analysis model names are safe to surface on the LLM entry.
    providers_out["llm"]["writing_model"] = settings.llm_model
    providers_out["llm"]["analysis_model"] = (
        settings.llm_model_analysis or settings.llm_model
    )
    return {"providers": providers_out}


async def _check_all(providers) -> dict:
    async def _one(name: str, provider) -> dict:
        reachable = False
        try:
            if provider is not None:
                reachable = bool(await provider.health_check())
        except Exception:  # noqa: BLE001 - a status check must never raise
            logger.warning(
                "provider_status_check_failed",
                extra={"event": "provider_status_check_failed", "provider": name},
            )
        finally:
            try:
                await provider.aclose()
            except Exception:  # noqa: BLE001
                pass
        return {"reachable": reachable}

    results: dict[str, dict] = {}
    for name, attr in _ORDER:
        results[name] = await _one(name, getattr(providers, attr))
    return results
