"""P7 unit: Strapi payload builders (spec sections 6.8, 37, 39, 40)."""

import pytest

from app.schemas.article import ArticleDocument
from app.schemas.strapi import (
    SEO_KEYWORDS_MAX_CHARS,
    SEO_KEYWORDS_MAX_COUNT,
    build_draft_payload,
    build_final_body_payload,
    build_seo_keywords,
    payload_hash,
    resolve_media_url,
    serialize_relation,
)


def _doc(**overrides) -> ArticleDocument:
    base = dict(
        title="Anxious Attachment No Contact: The Test Guide",
        body_markdown="## Intro\n\nBody text.",
        seo_title="Anxious Attachment and No Contact",
        meta_description="Understand why no contact can feel so intense.",
        slug="anxious-attachment-no-contact",
        primary_keyword="anxious attachment no contact",
        secondary_keywords=["anxious attachment", "no contact rule"],
        long_tail_keywords=["why is no contact so hard"],
        search_intent="informational",
        article_strategy="listicle",
    )
    base.update(overrides)
    return ArticleDocument(**base)


# ------------------------------------------------------------------ seoKeywords
def test_seo_keywords_priority_and_order():
    out = build_seo_keywords(
        "anxious attachment no contact",
        ["anxious attachment", "no contact rule"],
        ["why is no contact so hard"],
    )
    assert out == (
        "anxious attachment no contact, anxious attachment, "
        "no contact rule, why is no contact so hard"
    )


def test_seo_keywords_dedupes_case_insensitively():
    out = build_seo_keywords(
        "Anxious Attachment",
        ["anxious attachment", "ANXIOUS attachment"],
        ["other keyword"],
    )
    assert out == "Anxious Attachment, other keyword"


def test_seo_keywords_max_12():
    kws = [f"kw-{i}" for i in range(20)]
    out = build_seo_keywords("primary", kws[:15], kws[15:])
    assert len(out.split(", ")) == SEO_KEYWORDS_MAX_COUNT


def test_seo_keywords_max_chars():
    # Each keyword ~45 chars; 12 would be ~540 chars > 500.
    kw = "a" * 45
    out = build_seo_keywords(kw, [kw * 1 for _ in range(12)], [])
    assert len(out) <= SEO_KEYWORDS_MAX_CHARS
    # every included keyword is the full 45-char one
    assert all(part == kw for part in out.split(", "))


def test_seo_keywords_empty_secondary_longtail():
    assert build_seo_keywords("primary", None, None) == "primary"


def test_seo_keywords_skips_blanks():
    assert build_seo_keywords("", ["  ", "ok"], ["", "fine"]) == "ok, fine"


# -------------------------------------------------------------- draft payload
def test_draft_payload_shape():
    payload = build_draft_payload(
        _doc(),
        author_document_id="auth-1",
        category_document_id="cat-1",
        body="## Intro\n\nTemporary body.",
    )
    data = payload["data"]
    assert set(payload) == {"data"}
    assert data["title"] == "Anxious Attachment No Contact: The Test Guide"
    assert data["slug"] == "anxious-attachment-no-contact"
    assert data["body"] == "## Intro\n\nTemporary body."
    assert data["metaTitle"] == "Anxious Attachment and No Contact"
    assert data["metaDescription"].startswith("Understand why")
    assert data["seoKeywords"] == (
        "anxious attachment no contact, anxious attachment, no contact rule, "
        "why is no contact so hard"
    )
    assert data["author"] == "auth-1"
    assert data["category"] == "cat-1"
    # section 6.5: postedAt NOT written on drafts by default
    assert "postedAt" not in data


def test_draft_payload_posted_at_only_when_enabled():
    off = build_draft_payload(
        _doc(),
        author_document_id="a",
        category_document_id="c",
        body="b",
        include_posted_at=False,
        posted_at="2026-09-14",
    )
    assert "postedAt" not in off["data"]

    on = build_draft_payload(
        _doc(),
        author_document_id="a",
        category_document_id="c",
        body="b",
        include_posted_at=True,
        posted_at="2026-09-14",
    )
    assert on["data"]["postedAt"] == "2026-09-14"


def test_draft_payload_none_relations_omitted():
    data = build_draft_payload(
        _doc(), author_document_id=None, category_document_id=None, body="b"
    )["data"]
    assert data["author"] is None
    assert data["category"] is None


def test_final_body_payload_only_body():
    assert build_final_body_payload("final body") == {"data": {"body": "final body"}}


# -------------------------------------------------------------------- hashing
def test_payload_hash_is_sha256_hex():
    digest = payload_hash({"data": {"body": "x"}})
    assert len(digest) == 64
    int(digest, 16)  # hex
    assert payload_hash({"data": {"body": "x"}}) == digest
    assert payload_hash({"data": {"body": "y"}}) != digest


# ---------------------------------------------------------------- media URLs
def test_resolve_media_url_relative_gets_base_prefix():
    assert (
        resolve_media_url("/uploads/hero.webp", "https://cms.test")
        == "https://cms.test/uploads/hero.webp"
    )
    assert (
        resolve_media_url("/uploads/hero.webp", "https://cms.test/")
        == "https://cms.test/uploads/hero.webp"
    )


def test_resolve_media_url_absolute_passes_through():
    assert (
        resolve_media_url("https://cdn.test/hero.webp", "https://cms.test")
        == "https://cdn.test/hero.webp"
    )


def test_resolve_media_url_no_base():
    assert resolve_media_url("/uploads/a.png", "") == "/uploads/a.png"
    assert resolve_media_url("", "https://cms.test") == ""


# -------------------------------------------------------------------- relations
def test_serialize_relation_short_document_id():
    assert serialize_relation("  auth-123  ") == "auth-123"
    assert serialize_relation(None) is None
    assert serialize_relation("") is None
    assert serialize_relation("   ") is None
