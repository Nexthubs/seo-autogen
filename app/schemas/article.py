"""Article domain model (SEO-AUTO-DEV-SPEC.md sections 5, 6.8, 24,
26, 29).

Hard rule: ``body_markdown`` never contains an H1. The article title is
the single H1, rendered by the frontend from ``Blog.title``.
"""

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

#: Max keywords for Strapi seoKeywords field (spec section 6.8).
SEO_KEYWORDS_MAX_COUNT = 12
SEO_KEYWORDS_MAX_CHARS = 500

#: Setext H1: a non-blank text line followed by an ``=`` underline (a
#: ``-`` underline is a Setext *H2*, not an H1 — matching only ``=`` keeps
#: horizontal rules and H2 underlines out of it).
_SETEXT_H1 = re.compile(
    r"(?m)^[^\n`#*\s][^\n]*[^\n`\s]\n[ \t]*={2,}[ \t]*\r?$"
)


class ArticleDocument(BaseModel):
    title: str
    body_markdown: str

    seo_title: str
    meta_description: str
    slug: str

    primary_keyword: str
    secondary_keywords: list[str] = []
    long_tail_keywords: list[str] = []

    search_intent: str
    article_strategy: str

    target_function: str | None = None

    @field_validator("body_markdown")
    @classmethod
    def body_must_not_contain_h1(cls, value: str) -> str:
        for line in value.splitlines():
            stripped = line.strip()
            if stripped.startswith("# ") or stripped == "#":
                raise ValueError(
                    "body_markdown must not contain an H1; "
                    "the title is the only H1 (spec section 5)"
                )
        if _SETEXT_H1.search(value):
            raise ValueError(
                "body_markdown must not contain a Setext H1 (= underline); "
                "the title is the only H1 (spec section 5)"
            )
        return value

    @field_validator("slug")
    @classmethod
    def slug_must_be_kebab(cls, value: str) -> str:
        if not value or any(ch.isspace() for ch in value):
            raise ValueError("slug must be non-empty kebab-case without spaces")
        return value

    @property
    def word_count(self) -> int:
        return len(self.body_markdown.split())

    @property
    def seo_keywords(self) -> str:
        """Comma-joined seoKeywords for Strapi (spec section 6.8).

        Priority: primary, secondary, long-tail. Deduped, max 12,
        ~500 chars.
        """
        seen: set[str] = set()
        ordered: list[str] = []
        for kw in [self.primary_keyword, *self.secondary_keywords, *self.long_tail_keywords]:
            kw = kw.strip()
            if not kw or kw.lower() in seen:
                continue
            seen.add(kw.lower())
            ordered.append(kw)
        joined = ", ".join(ordered[:SEO_KEYWORDS_MAX_COUNT])
        if len(joined) > SEO_KEYWORDS_MAX_CHARS:
            while ordered and len(", ".join(ordered)) > SEO_KEYWORDS_MAX_CHARS:
                ordered.pop()
            joined = ", ".join(ordered)
        return joined


class ArticleDraftOutput(BaseModel):
    """Raw JSON the ArticleWriter / ArticleReviser must return
    (sections 24, 27). Post-processing (title from the outline, H1
    strip, slug normalization) happens program-side.
    """

    title: str = ""
    body_markdown: str = ""
    seo_title: str = ""
    meta_description: str = ""
    slug: str = ""


# ============================================================
# 26. Reviewer pipeline
# ============================================================
class SEOReview(BaseModel):
    """SEO review of a full article (section 26.1)."""

    total_score: int
    keyword_score: int
    search_intent_score: int
    structure_score: int
    readability_score: int
    cta_score: int

    issues: list[str] = Field(default_factory=list)
    required_changes: list[str] = Field(default_factory=list)


class FactIssue(BaseModel):
    """One factual claim judged against the evidence notes (section 26.2)."""

    quote_or_claim: str
    verdict: Literal["supported", "soften", "remove", "needs_source"]
    reason: str


class FactReview(BaseModel):
    """Fact review result. Competitor "studies show…" is NOT evidence;
    only the job's evidence notes are (section 26.2).
    """

    issues: list[FactIssue] = Field(default_factory=list)


class StyleReview(BaseModel):
    """Human style review (section 26.3)."""

    score: int
    ai_patterns: list[str] = Field(default_factory=list)
    repetitive_patterns: list[str] = Field(default_factory=list)
    weak_sections: list[str] = Field(default_factory=list)
    required_changes: list[str] = Field(default_factory=list)


# ============================================================
# 29. Anti-copy check
# ============================================================
class AntiCopyMatch(BaseModel):
    """One flagged overlap between the draft and a competitor source
    (section 29): a long near-exact fragment.
    """

    draft_phrase: str
    source_url: str
    matched_phrase: str
    similarity: float
    word_count: int


class AntiCopyReport(BaseModel):
    """Result of the programmatic anti-copy check (section 29).

    Overlaps are flags for the Reviser, never a pass/fail "duplication
    rate".
    """

    matches: list[AntiCopyMatch] = Field(default_factory=list)
    sources_compared: int = 0
    # Flagged when any match reaches the serious threshold.
    has_serious_overlap: bool = False
