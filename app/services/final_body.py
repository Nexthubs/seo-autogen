"""Shared final-body renderer (SEO-AUTO-DEV-SPEC.md sections 21 + 33, H07).

The final article body — the local ``article.md`` export (P6) AND the
Strapi Draft body (P7) — must have BOTH marker kinds resolved before it
leaves the pipeline:

    1. ``[[INTERNAL_LINK:X]]`` -> ``[anchor](target_url)``  (section 21)
    2. ``[[IMAGE:N]]``        -> ``![alt](src)``            (section 33)

H07: the two final paths used to resolve only image markers, so the raw
``[[INTERNAL_LINK:*]]`` markers the Writer emits were silently written to
``article.md`` and PUT to Strapi. This is the ONE place the final body is
assembled — export and sync both call it, internal links first, then
images, then a hard assertion that no marker of either kind survives.

A marker that cannot be resolved is a pipeline error, not a silent pass:
the DoD gate (section 63) is supposed to reject an article whose markers
are unknown/inactive, so reaching this point with one is a contract
violation that must fail loudly.
"""

import logging

from sqlalchemy.orm import Session

from app.core.exceptions import ErrorCode, PipelineError
from app.schemas.internal_link import MARKER_SYNTAX
from app.services.image_markers import (
    MARKER_PATTERN as IMAGE_MARKER_PATTERN,
    insert_image_markers,
    resolve_image_markers,
)
from app.services.internal_link_service import resolve_markers

logger = logging.getLogger(__name__)


def render_final_body(
    session: Session,
    body_markdown: str,
    *,
    images: dict[str, tuple[str, str]],
    placements: list[tuple[str, str | None]],
    include_hero: tuple[str, str] | None = None,
) -> str:
    """Assemble the final article body with every marker resolved (H07).

    ``images`` maps insertion_marker -> (alt_text, src); ``placements`` is
    ``(insertion_marker, section_heading)`` in reading order (inline
    items only); ``include_hero`` prepends the hero when the frontend
    does not render ``mainImage`` (section 3.2).

    Raises :class:`PipelineError` (``ARTICLE_VALIDATION_FAILED``) when a
    marker cannot be resolved instead of shipping it raw.
    """
    # 1) internal links FIRST (section 21): marker -> validated rule -> link
    linked, _links, validation = resolve_markers(session, body_markdown)
    if not validation.valid:
        details = ", ".join(
            list(validation.unknown_markers) + list(validation.inactive_markers)
        )
        logger.warning(
            "final_body_unresolvable_internal_links",
            extra={
                "event": "final_body_unresolvable_internal_links",
                "unknown": validation.unknown_markers,
                "inactive": validation.inactive_markers,
            },
        )
        raise PipelineError(
            ErrorCode.ARTICLE_VALIDATION_FAILED,
            f"final render: unresolvable internal link markers ({details}) — "
            "the article cannot be exported/synced with raw markers",
        )

    # 2) images SECOND (section 33): insert then resolve markers
    marked = insert_image_markers(linked, placements)
    out = resolve_image_markers(marked, images, include_hero=include_hero)

    # 3) HARD assertion (H07): no residual marker of either kind.
    #    resolve_image_markers drops unknown image markers, and step 1
    #    already raised on unknown/inactive links, so this normally never
    #    fires — it is the last line of defence against a raw marker leak.
    residual_link = MARKER_SYNTAX.search(out)
    residual_image = IMAGE_MARKER_PATTERN.search(out)
    residual = residual_link or residual_image
    if residual:
        raise PipelineError(
            ErrorCode.ARTICLE_VALIDATION_FAILED,
            f"final render: residual marker {residual.group(0)!r} left in "
            "the body — internal-link/image resolution failed",
        )
    return out
