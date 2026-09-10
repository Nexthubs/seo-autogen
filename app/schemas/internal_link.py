"""Internal link rule schemas (SEO-AUTO-DEV-SPEC.md section 21, 46.4)."""

import re

from pydantic import BaseModel, Field

#: Marker shape: uppercase letters, digits, underscore (e.g. ATTACHMENT_TEST).
MARKER_PATTERN = re.compile(r"^[A-Z0-9_]{1,64}$")

#: Marker syntax as it appears in article markdown.
MARKER_SYNTAX = re.compile(r"\[\[INTERNAL_LINK:([A-Z0-9_]{1,64})\]\]")


def is_valid_marker(marker: str) -> bool:
    return bool(MARKER_PATTERN.match(marker or ""))


def extract_markers(markdown: str) -> list[str]:
    """All markers referenced in a markdown body, in order of appearance."""
    return MARKER_SYNTAX.findall(markdown or "")


class InternalLinkRule(BaseModel):
    """One allowed internal link marker (section 21)."""

    marker: str = Field(description="Stable marker id, e.g. ATTACHMENT_TEST")
    anchor_text: str | None = None
    target_url: str
    topic: str | None = None
    keywords: list[str] = Field(default_factory=list)
    active: bool = True


class ResolvedInternalLink(BaseModel):
    """Marker resolved to a concrete markdown link (section 21 pipeline)."""

    marker: str
    anchor_text: str
    target_url: str


class InternalLinkValidation(BaseModel):
    """Validation result for the markers used in an article (section 63)."""

    valid: bool
    used_markers: list[str] = Field(default_factory=list)
    unknown_markers: list[str] = Field(default_factory=list)
    inactive_markers: list[str] = Field(default_factory=list)
