"""Prompt version dashboard API (P9-B2 — spec section 48).

``GET /api/prompts`` returns the prompt inventory (name / version / hash /
size / step), per-prompt usage aggregates from the six provenance result
tables, historical (stale) prompt versions that no longer match the file on
disk, and the two unversioned rulebooks (content hash only).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.prompt_dashboard import prompt_dashboard_payload

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


@router.get("")
def api_prompts(session: Session = Depends(get_db)) -> dict:
    """Prompt inventory + usage (spec 47/48)."""
    return prompt_dashboard_payload(session)
