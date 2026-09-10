"""Strapi-facing schemas (SEO-AUTO-DEV-SPEC.md sections 35-42, 6).

P1 defined ``MediaUploadResult`` (referenced by the ``CMSProvider``
interface). P7 completes it: the draft payload builders (section 37),
the seoKeywords serializer (section 6.8), the relation serializer
(section 37, Strapi 5 short documentId) and media URL resolution
(section 39).

Security (section 60): nothing here ever carries the API token; the
``CMSProvider`` implementation adds the ``Authorization`` header
per request and never logs it.
"""

import hashlib
import json

from pydantic import BaseModel

from app.schemas.article import ArticleDocument


class MediaUploadResult(BaseModel):
    """Result of a Strapi /api/upload call."""

    media_id: int
    url: str
    document_id: str | None = None
    alternative_text: str | None = None


class StrapiBlogEntry(BaseModel):
    """One Blog entry as returned by ``GET /api/blogs`` (section 42)."""

    id: int
    document_id: str
    slug: str | None = None
    title: str | None = None
    status: str | None = None
    body: str | None = None
    meta_title: str | None = None
    meta_description: str | None = None
    seo_keywords: str | None = None
    author: str | int | None = None
    category: str | int | None = None
    main_image: str | None = None


# ======================================================================
# seoKeywords (section 6.8)
# ======================================================================

#: Section 6.8: max 12 keywords, ~500 chars total.
SEO_KEYWORDS_MAX_COUNT = 12
SEO_KEYWORDS_MAX_CHARS = 500


def build_seo_keywords(
    primary: str,
    secondary: list[str] | None = None,
    long_tail: list[str] | None = None,
) -> str:
    """Comma-separated seoKeywords string (section 6.8).

    Source priority: Primary -> Secondary -> selected Long-tail.
    Deduped (case-insensitive, order preserved), capped at 12
    keywords and ~500 characters.
    """
    ordered: list[str] = []
    for keyword in [primary, *(secondary or []), *(long_tail or [])]:
        text = (keyword or "").strip()
        if not text:
            continue
        if any(t.lower() == text.lower() for t in ordered):
            continue
        ordered.append(text)

    out: list[str] = []
    total = 0
    for keyword in ordered[:SEO_KEYWORDS_MAX_COUNT]:
        delta = len(keyword) + (2 if out else 0)  # ", " separator
        if out and total + delta > SEO_KEYWORDS_MAX_CHARS:
            break
        out.append(keyword)
        total += delta
    return ", ".join(out)


# ======================================================================
# Draft payload (section 37)
# ======================================================================


def serialize_relation(document_id: str | None) -> str | None:
    """Relation value for author/category (section 37).

    Strapi 5 many-to-one accepts the short documentId; the
    serializer is the single place where relation representation
    is fixed, so a Strapi 4 server difference is repaired here
    only (plus an integration test) — never in the pipeline.
    """
    return (document_id or "").strip() or None


def build_draft_payload(
    doc: ArticleDocument,
    *,
    author_document_id: str | None,
    category_document_id: str | None,
    body: str,
    include_posted_at: bool = False,
    posted_at: str | None = None,
) -> dict:
    """Full Blog draft payload: ``{"data": {...}}`` (section 37).

    ``title`` goes to Blog.title; ``body`` to Blog.body — the H1
    rule (section 5.1) is enforced by the caller, which passes
    ``doc.body_markdown`` (never a markdown H1 line).
    ``author``/``category`` use the short documentId (section 37).
    """
    data: dict = {
        "title": doc.title,
        "slug": doc.slug,
        "body": body,
        "metaTitle": doc.seo_title,
        "metaDescription": doc.meta_description,
        "seoKeywords": build_seo_keywords(
            doc.primary_keyword,
            doc.secondary_keywords,
            doc.long_tail_keywords,
        ),
        "author": serialize_relation(author_document_id),
        "category": serialize_relation(category_document_id),
    }
    # Section 6.5: postedAt is NOT written on drafts by default.
    if include_posted_at and posted_at:
        data["postedAt"] = posted_at
    return {"data": data}


def build_final_body_payload(body: str) -> dict:
    """STEP E payload: only the final body is updated (section 40)."""
    return {"data": {"body": body}}


def payload_hash(payload: dict) -> str:
    """sha256 hex digest of a payload (section 46.16: CHAR(64))."""
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


# ======================================================================
# Media URLs (section 39)
# ======================================================================


def resolve_media_url(url: str, base_url: str) -> str:
    """Absolute public URL for an uploaded media item (section 39).

    Strapi returns relative ``/uploads/...`` URLs; prefix them with
    ``STRAPI_BASE_URL``. Absolute URLs pass through unchanged.
    """
    url = (url or "").strip()
    if not url:
        return url
    if url.startswith(("http://", "https://")):
        return url
    base = (base_url or "").rstrip("/")
    if not url.startswith("/"):
        url = "/" + url
    return base + url
