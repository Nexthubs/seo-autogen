"""Research pipeline Pydantic schemas (SEO-AUTO-DEV-SPEC.md sections
16, 17, 18, 22, 23).

Every LLM intermediate result in the Research Pipeline is validated
against one of these models before it may be persisted (spec section
49 / P4 acceptance: "Pydantic validated, DB persisted").
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


# ============================================================
# 16. Competitor analysis (one per competitor source)
# ============================================================
class CompetitorAnalysis(BaseModel):
    """Structured analysis of a single competitor article (section 16).

    Only the CompetitorAnalyzer ever receives the full competitor
    text; downstream steps receive this structured digest instead.
    """

    source_id: UUID
    content_type: str
    search_intent: str
    estimated_word_count: int

    headings: list[str] = Field(default_factory=list)
    pain_points: list[str] = Field(default_factory=list)
    key_topics: list[str] = Field(default_factory=list)
    practical_advice: list[str] = Field(default_factory=list)
    faq_topics: list[str] = Field(default_factory=list)

    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    potential_gaps: list[str] = Field(default_factory=list)


# ============================================================
# 17. SERP synthesis
# ============================================================
class SERPSynthesis(BaseModel):
    """Cross-source synthesis of the SERP (section 17)."""

    dominant_intent: str
    secondary_intents: list[str] = Field(default_factory=list)

    common_topics: list[str] = Field(default_factory=list)
    common_pain_points: list[str] = Field(default_factory=list)
    common_questions: list[str] = Field(default_factory=list)

    content_patterns: list[str] = Field(default_factory=list)
    missing_topics: list[str] = Field(default_factory=list)
    opportunities: list[str] = Field(default_factory=list)


# ============================================================
# 18. Evidence research
# ============================================================
class EvidenceNote(BaseModel):
    """One factual evidence note (section 18).

    SEO competitor claims are NOT evidence. The Writer may only use
    research findings / numbers that exist in the evidence notes for
    the job.

    ``verification_status`` / ``supporting_excerpt`` (audit R-H06) record
    the outcome of the programmatic support check against the *fetched*
    source body: it is not enough that the source is reachable — the page
    must actually corroborate the proposed title/claim/numbers.
    """

    claim: str
    source_title: str
    source_url: str
    source_type: str
    confidence: Literal["high", "medium", "low"]
    usage: Literal["supported", "soften", "avoid"]
    note: str | None = None

    verification_status: Literal[
        "supported", "unsupported", "contradicted", "unverified"
    ] | None = None
    supporting_excerpt: str | None = None


# ============================================================
# 22. Content brief
# ============================================================
class ContentBrief(BaseModel):
    """Content brief: the contract between research and writing
    (section 22).
    """

    primary_keyword: str
    secondary_keywords: list[str] = Field(default_factory=list)
    long_tail_keywords: list[str] = Field(default_factory=list)

    search_intent: str
    search_stage: str
    article_strategy: str

    target_reader: str
    core_problem: str
    emotional_context: str

    unique_angle: str
    content_gaps: list[str] = Field(default_factory=list)

    required_topics: list[str] = Field(default_factory=list)
    faq_questions: list[str] = Field(default_factory=list)

    target_function: str | None = None
    cta_strategy: list[str] = Field(default_factory=list)

    internal_link_markers: list[str] = Field(default_factory=list)

    recommended_word_count: int


# ============================================================
# 23. Outline
# ============================================================
class OutlineSection(BaseModel):
    """One outline section (section 23)."""

    heading: str
    level: Literal[2, 3]
    purpose: str
    keywords: list[str] = Field(default_factory=list)
    cta_slot: bool = False


class ArticleOutline(BaseModel):
    """Structured article outline. The outline is a structured object,
    never just a Markdown string (section 23).
    """

    title: str
    sections: list[OutlineSection]
    faq_questions: list[str] = Field(default_factory=list)
