"""Strapi catalog routes (spec 44: ``/api/strapi/authors|categories``).

Feeds the Author/Category selects on the New Article form (43.1). Degrades
gracefully: with no API token the endpoints report ``available: false`` and
an empty list, so the form keeps working and the job falls back to the
configured ``strapi_default_*`` values (spec 11).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter

from app.core.config import get_settings
from app.providers.cms.strapi_cms import StrapiCMSProvider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/strapi", tags=["strapi"])


def _items(data: list[dict]) -> list[dict]:
    out = []
    for entry in data:
        attrs = entry.get("attributes") or {}
        document_id = entry.get("documentId") or str(entry.get("id", ""))
        label = (
            attrs.get("name")
            or attrs.get("title")
            or attrs.get("slug")
            or document_id
        )
        out.append({"document_id": document_id, "label": label})
    return out


async def _list(provider: StrapiCMSProvider, method: str) -> list[dict]:
    try:
        data = await getattr(provider, method)(page_size=100)
        return _items(data)
    finally:
        try:
            await provider.aclose()
        except Exception:  # noqa: BLE001
            pass


def _catalog(method: str) -> dict:
    settings = get_settings()
    if not settings.strapi_configured:
        return {"available": False, "reason": "not_configured", "items": []}
    provider = StrapiCMSProvider(settings=settings)
    try:
        items = asyncio.run(_list(provider, method))
    except Exception:  # noqa: BLE001 - a bad CMS must not break the form
        logger.warning(
            "strapi_catalog_failed",
            extra={"event": "strapi_catalog_failed", "method": method},
        )
        return {"available": False, "reason": "unreachable", "items": []}
    return {"available": True, "items": items}


@router.get("/authors")
def api_strapi_authors() -> dict:
    return _catalog("list_authors")


@router.get("/categories")
def api_strapi_categories() -> dict:
    return _catalog("list_categories")
