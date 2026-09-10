"""P1 schema validation tests (spec sections 13, 15, 24, 30)."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.article import ArticleDocument
from app.schemas.images import (
    GeneratedImage,
    ImageGenerationRequest,
    ImagePlan,
    ImagePlanItem,
)
from app.schemas.serp import (
    OrganicResult,
    PAAQuestion,
    SERPRequest,
    SERPResponse,
)
from app.schemas.sources import ExtractedPage

# ----------------------------------------------------------------------
# SERP (spec section 13)
# ----------------------------------------------------------------------
def test_serp_request_defaults():
    req = SERPRequest(keyword="best crm", location_code=2840, language_code="en")
    assert req.device == "desktop"
    assert req.depth == 10


def test_serp_request_rejects_bad_device():
    with pytest.raises(ValidationError):
        SERPRequest(
            keyword="x", location_code=1, language_code="en", device="tablet"
        )


def test_serp_response_full():
    resp = SERPResponse(
        keyword="best crm",
        organic_results=[
            OrganicResult(
                rank=1,
                title="Top CRM",
                url="https://example.com/crm",
                domain="example.com",
                snippet="snippet",
            )
        ],
        paa_questions=[PAAQuestion(question="What is CRM?")],
        related_searches=["crm software"],
        raw={"provider": "dataforseo"},
    )
    assert resp.organic_results[0].rank == 1
    assert resp.paa_questions[0].source_url is None
    assert resp.raw == {"provider": "dataforseo"}


# ----------------------------------------------------------------------
# Sources (spec section 15)
# ----------------------------------------------------------------------
def test_extracted_page():
    now = datetime.now(timezone.utc)
    page = ExtractedPage(
        url="https://example.com/a",
        normalized_url="https://example.com/a",
        title="A",
        content_markdown="one two three",
        extracted_at=now,
        extractor="exa",
    )
    assert page.word_count == 3
    assert page.title == "A"


# ----------------------------------------------------------------------
# Article (spec sections 5, 24)
# ----------------------------------------------------------------------
def _article(**overrides) -> dict:
    base = dict(
        title="My Title",
        body_markdown="## Section\n\nContent here.",
        seo_title="SEO Title",
        meta_description="Meta",
        slug="my-title",
        primary_keyword="best crm",
        secondary_keywords=["crm software"],
        long_tail_keywords=["best crm for small business"],
        search_intent="commercial",
        article_strategy="comparison",
    )
    base.update(overrides)
    return base


def test_article_document_ok():
    doc = ArticleDocument(**_article())
    assert doc.target_function is None
    assert doc.word_count >= 2


def test_article_document_rejects_h1_in_body():
    with pytest.raises(ValidationError, match="H1"):
        ArticleDocument(**_article(body_markdown="# Bad H1\n\ntext"))


def test_article_document_rejects_bare_hash():
    with pytest.raises(ValidationError):
        ArticleDocument(**_article(body_markdown="#\n\ntext"))


def test_article_document_allows_h2_and_later():
    doc = ArticleDocument(
        **_article(body_markdown="## H2\n\n### H3\n\ntext")
    )
    assert doc.body_markdown.startswith("## H2")


def test_article_document_rejects_slug_with_space():
    with pytest.raises(ValidationError):
        ArticleDocument(**_article(slug="my title"))


def test_seo_keywords_property():
    doc = ArticleDocument(
        **_article(
            # duplicates the primary keyword on purpose
            secondary_keywords=["best crm", "crm software"],
        )
    )
    # deduped (case-insensitive), primary first
    assert doc.seo_keywords == (
        "best crm, crm software, best crm for small business"
    )


# ----------------------------------------------------------------------
# Images (spec sections 10.4, 30)
# ----------------------------------------------------------------------
def test_image_models():
    req = ImageGenerationRequest(prompt="a photo", filename="hero.png")
    img = GeneratedImage(
        local_path="/data/hero.png",
        filename="hero.png",
        mime_type="image/png",
        prompt="a photo",
        provider="openai",
    )
    assert req.aspect_ratio == "16:9"
    assert img.provider_request_id is None


def _item(role="hero", **kw) -> ImagePlanItem:
    base = dict(
        role=role,
        purpose="hero image",
        filename="hero.png",
        alt_text="alt",
        prompt="prompt",
        aspect_ratio="16:9",
        insertion_marker="<!--img:1-->" if role == "inline" else None,
    )
    base.update(kw)
    return ImagePlanItem(**base)


def test_image_plan_valid():
    plan = ImagePlan(
        total_count=2,
        images=[_item(), _item(role="inline")],
    )
    assert plan.total_count == 2


def test_image_plan_rejects_count_out_of_bounds():
    with pytest.raises(ValidationError):
        ImagePlan(total_count=0, images=[])
    with pytest.raises(ValidationError):
        ImagePlan(total_count=4, images=[_item(), _item(), _item(), _item()])


def test_image_plan_rejects_non_hero_first():
    with pytest.raises(ValidationError, match="hero"):
        ImagePlan(total_count=1, images=[_item(role="inline")])


def test_image_plan_rejects_inline_without_marker():
    with pytest.raises(ValidationError, match="insertion_marker"):
        ImagePlan(
            total_count=2,
            images=[_item(), _item(role="inline", insertion_marker=None)],
        )
