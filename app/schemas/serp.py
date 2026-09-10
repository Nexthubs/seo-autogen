"""SERP domain models (SEO-AUTO-DEV-SPEC.md section 13)."""

from typing import Literal

from pydantic import BaseModel, Field


class SERPRequest(BaseModel):
    keyword: str
    location_code: int
    language_code: str
    device: Literal["desktop", "mobile"] = "desktop"
    depth: int = 10


class OrganicResult(BaseModel):
    rank: int
    title: str
    url: str
    domain: str | None = None
    snippet: str | None = None


class PAAQuestion(BaseModel):
    question: str
    source_url: str | None = None


class FeaturedSnippet(BaseModel):
    """Google's featured snippet (audit H01 — official DataForSEO contract)."""

    title: str | None = None
    url: str | None = None
    domain: str | None = None
    snippet: str | None = None


class SERPResponse(BaseModel):
    keyword: str
    organic_results: list[OrganicResult] = Field(default_factory=list)
    paa_questions: list[PAAQuestion] = Field(default_factory=list)
    related_searches: list[str] = Field(default_factory=list)
    #: Google's featured snippet, when present in the SERP (audit H01).
    featured_snippet: FeaturedSnippet | None = None
    # Full provider payload kept for audit/debug (spec section 12.2).
    raw: dict = Field(default_factory=dict)
    #: Cost (USD) reported by the paid SERP provider (spec section 54).
    #: None when the provider reports no cost (or the response lacks it).
    provider_cost: float | None = None
