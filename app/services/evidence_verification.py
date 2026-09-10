"""Evidence source verification (SEO-AUTO-DEV-SPEC.md sections 18, 25;
content guideline section 28: cited research must be verified to exist).

The LLM proposes evidence notes (claim + source URL + confidence). Before
those notes are trusted by the writer / fact reviewer, each note's source
URL is checked through an *independent* fetch channel (the content
extractor — not the same LLM that produced the note). A source that cannot
be fetched, or that returns empty content, is downgraded to
``usage="avoid"`` and ``confidence="low"`` so it cannot ship as a citation.

This is a mock-friendly contract: the orchestrator passes the configured
content extractor; unit tests pass a fake (or ``None`` to skip).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.schemas.research import EvidenceNote

if TYPE_CHECKING:  # pragma: no cover
    from app.providers.extractor.base import ContentExtractor

logger = logging.getLogger(__name__)


async def verify_evidence_sources(
    notes: list[EvidenceNote],
    verifier: "ContentExtractor | None",
) -> list[EvidenceNote]:
    """Return the notes with unverifiable sources downgraded.

    When ``verifier`` is ``None`` the notes pass through unchanged (no
    provider wired, e.g. focused unit tests). Otherwise each note's
    ``source_url`` is fetched; a note whose source is missing, unreachable,
    or empty is returned as a copy with ``confidence="low"`` and
    ``usage="avoid"`` plus a ``source_unverified`` note appended.
    """
    if verifier is None:
        return list(notes)

    verified: list[EvidenceNote] = []
    for note in notes:
        ok, reason = await _verify_one(verifier, note)
        if ok:
            verified.append(note)
        else:
            logger.warning(
                "evidence_source_unverified",
                extra={
                    "event": "evidence_source_unverified",
                    "source_url": note.source_url,
                    "reason": reason,
                },
            )
            verified.append(
                note.model_copy(
                    update={
                        "confidence": "low",
                        "usage": "avoid",
                        "note": _append_reason(note.note, f"source_unverified: {reason}"),
                    }
                )
            )
    return verified


async def _verify_one(
    verifier: "ContentExtractor", note: EvidenceNote
) -> tuple[bool, str]:
    """Fetch the note's source URL; return (reachable_with_content, reason)."""
    if not note.source_url:
        return False, "missing source_url"
    try:
        pages = await verifier.extract([note.source_url])
    except Exception as exc:  # noqa: BLE001 — any fetch failure means "unverified"
        return False, f"fetch failed: {exc.__class__.__name__}"
    if not pages:
        return False, "no content returned"
    content = (pages[0].content_markdown or "").strip()
    if not content:
        return False, "empty content"
    return True, "ok"


def _append_reason(existing: str | None, addition: str) -> str:
    if existing:
        return f"{existing}; {addition}"
    return addition
