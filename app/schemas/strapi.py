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
    """One Blog entry as returned by ``GET /api/blogs`` (section 42).

    H03: Strapi 5 returns entries FLAT (``id``/``documentId``/``title``/…
    directly under ``data`` — no ``attributes`` wrapper). Relations can
    come back as a short documentId string, a numeric id, or a POPULATED
    object (``{"id": …, "documentId": …, "name": …}``) when the request
    used a ``populate`` query; the provider parses all of these, and
    :func:`relation_document_id` / :func:`media_url` normalize them for
    verification (section 64).
    """

    id: int
    document_id: str
    slug: str | None = None
    title: str | None = None
    status: str | None = None
    body: str | None = None
    meta_title: str | None = None
    meta_description: str | None = None
    seo_keywords: str | None = None
    author: str | int | dict | None = None
    category: str | int | dict | None = None
    main_image: str | dict | None = None


def relation_document_id(value: str | int | dict | None) -> str | None:
    """Canonical documentId for an author/category relation value.

    Accepts the short documentId (string), a numeric Strapi id, or a
    populated relation object. Returns ``None`` for ``None``/blank —
    that is *not* a mismatch: when a relation was not written (e.g.
    author optional + no default), the GET must not fail on it.
    """
    if value is None or isinstance(value, (int, str)):
        return str(value) if value not in (None, "") else None
    if isinstance(value, dict):
        doc_id = value.get("documentId") or value.get("document_id")
        if doc_id:
            return str(doc_id)
        item_id = value.get("id")
        return str(item_id) if item_id is not None else None
    return None


def media_url(value: str | dict | None) -> str | None:
    """Canonical public URL for a media field value (H03/M02).

    Strapi returns media either as a plain URL string or, when the
    response is populated, as an object with ``url``/``id``/``documentId``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        return value.get("url") or None
    return None


def media_id(value: str | dict | None) -> int | None:
    """Canonical media item id for a media field value (H03/M02)."""
    if value is None:
        return None
    if isinstance(value, (int, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, dict):
        item_id = value.get("id")
        try:
            return int(item_id) if item_id is not None else None
        except (TypeError, ValueError):
            return None
    return None


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
