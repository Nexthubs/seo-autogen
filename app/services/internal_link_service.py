"""Internal link service (SEO-AUTO-DEV-SPEC.md section 21, P3).

Pipeline contract:
1. The LLM may ONLY insert markers ``[[INTERNAL_LINK:MARKER]]`` — never URLs.
2. Final rendering: validate marker syntax → resolve against active rules →
   replace with a markdown link ``[anchor](target_url)``.
3. Markers that are unknown or inactive are reported; rendering leaves them
   in place so the problem is visible instead of silently dropping links.
"""

import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.internal_link import InternalLinkRule
from app.schemas.internal_link import (
    MARKER_SYNTAX,
    InternalLinkRule as InternalLinkRuleSchema,
    InternalLinkValidation,
    ResolvedInternalLink,
    extract_markers,
    is_valid_marker,
)

logger = logging.getLogger(__name__)


def upsert_rule(
    session: Session, rule: InternalLinkRuleSchema
) -> InternalLinkRule:
    """Insert or update a rule; markers are unique and case-sensitive."""
    if not is_valid_marker(rule.marker):
        raise ValueError(
            f"invalid marker {rule.marker!r}: expected 1-64 chars of [A-Z0-9_]"
        )
    existing = session.scalar(
        select(InternalLinkRule).where(InternalLinkRule.marker == rule.marker)
    )
    if existing is None:
        existing = InternalLinkRule(
            marker=rule.marker,
            anchor_text=rule.anchor_text,
            target_url=rule.target_url,
            topic=rule.topic,
            keywords=rule.keywords,
            active=rule.active,
        )
        session.add(existing)
    else:
        existing.anchor_text = rule.anchor_text
        existing.target_url = rule.target_url
        existing.topic = rule.topic
        existing.keywords = rule.keywords
        existing.active = rule.active
    session.flush()
    return existing


def list_rules(
    session: Session, active_only: bool = False
) -> list[InternalLinkRule]:
    stmt = select(InternalLinkRule).order_by(InternalLinkRule.marker)
    if active_only:
        stmt = stmt.where(InternalLinkRule.active.is_(True))
    return list(session.scalars(stmt))


def validate_markers(session: Session, markdown: str) -> InternalLinkValidation:
    """Check every marker used in the article against the rule set."""
    used = extract_markers(markdown)
    rules = {r.marker: r for r in list_rules(session)}
    unknown = [m for m in used if m not in rules]
    inactive = [m for m in used if m in rules and not rules[m].active]
    return InternalLinkValidation(
        valid=not unknown and not inactive,
        used_markers=used,
        unknown_markers=unknown,
        inactive_markers=inactive,
    )


def resolve_markers(
    session: Session, markdown: str, replace_unknown: bool = True
) -> tuple[str, list[ResolvedInternalLink], InternalLinkValidation]:
    """Marker → validate → resolve → markdown link (section 21).

    Returns (rendered_markdown, resolved_links, validation).
    """
    rules = {r.marker: r for r in list_rules(session)}
    validation = InternalLinkValidation(
        valid=True,
        used_markers=extract_markers(markdown),
        unknown_markers=[],
        inactive_markers=[],
    )
    resolved: list[ResolvedInternalLink] = []

    def _sub(match: re.Match[str]) -> str:
        marker = match.group(1)
        rule = rules.get(marker)
        if rule is None:
            validation.unknown_markers.append(marker)
            validation.valid = False
            return match.group(0) if replace_unknown else f"<!-- UNRESOLVED LINK {marker} -->"
        if not rule.active:
            validation.inactive_markers.append(marker)
            validation.valid = False
            return match.group(0) if replace_unknown else f"<!-- INACTIVE LINK {marker} -->"
        anchor = rule.anchor_text or marker
        link = ResolvedInternalLink(
            marker=marker, anchor_text=anchor, target_url=rule.target_url
        )
        resolved.append(link)
        return f"[{anchor}]({rule.target_url})"

    rendered = MARKER_SYNTAX.sub(_sub, markdown or "")
    if validation.unknown_markers or validation.inactive_markers:
        logger.warning(
            "internal_link_markers_unresolved",
            extra={
                "event": "internal_link_markers_unresolved",
                "unknown": validation.unknown_markers,
                "inactive": validation.inactive_markers,
            },
        )
    return rendered, resolved, validation
