"""Strapi Draft sync step (SEO-AUTO-DEV-SPEC.md sections 35-42, 64).

Two-phase Draft sync (section 36):

    A  create text-only Draft (or UPDATE the existing one) -> id + documentId
    B  upload Hero via entry linking -> mainImage
    C  upload inline images -> media URLs
    D  replace ``[[IMAGE:*]]`` markers with Strapi absolute URLs
    E  PUT the final body (explicit ``?status=draft``)
    F  GET the Draft and verify all 9 fields (section 64)

Idempotency (section 41): the ``strapi_syncs`` row anchors the job;
a retry with a stored ``strapi_document_id`` UPDATES that draft —
never creates a second one. Slug collisions (section 42) raise
``STRAPI_SLUG_CONFLICT`` before any write; the slug is never
auto-suffixed.

Failure semantics (section 64): a mid-sync failure (e.g. image
upload halfway through) persists ``sync_status=failed`` while
KEEPING ``strapi_document_id`` so the retry updates the same draft.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.enums import JobStatus, StrapiSyncStatus
from app.core.exceptions import ErrorCode, PipelineError
from app.db.models.images import ImageRow
from app.db.models.job import GenerationJob
from app.db.models.strapi_syncs import StrapiSyncRow
from app.pipeline.steps._article_common import (
    latest_article_version,
    load_research_context,
)
from app.pipeline.steps.image_generate import load_planned_images
from app.schemas.article import ArticleDocument
from app.schemas.strapi import (
    build_draft_payload,
    build_final_body_payload,
    payload_hash,
    resolve_media_url,
)
from app.services.image_markers import (
    insert_image_markers,
    resolve_image_markers,
)

logger = logging.getLogger(__name__)


def _rebuild_document(version, brief: dict, job: GenerationJob) -> ArticleDocument:
    """ArticleDocument from the persisted version + brief (section 6)."""
    return ArticleDocument(
        title=version.title,
        body_markdown=version.body_markdown,
        seo_title=version.seo_title,
        meta_description=version.meta_description,
        slug=version.slug,
        primary_keyword=(brief or {}).get("primary_keyword", ""),
        secondary_keywords=list((brief or {}).get("secondary_keywords", [])),
        long_tail_keywords=list((brief or {}).get("long_tail_keywords", [])),
        search_intent=(brief or {}).get("search_intent", ""),
        article_strategy=(brief or {}).get("article_strategy", ""),
        target_function=job.target_function,
    )


def _resolve_relation(
    job_value: str | None,
    default: str | None,
) -> str | None:
    """job override > default (section 6.3 / 6.4)."""
    return ((job_value or default) or "").strip() or None


def _fail_pre_sync(
    session: Session,
    job: GenerationJob,
    row: StrapiSyncRow | None,
    error: PipelineError,
) -> None:
    """Persist failure state before/around the HTTP phase (section 9)."""
    if row is not None:
        row.sync_status = StrapiSyncStatus.FAILED.value
        row.error_message = str(error)
    job.error_code = error.error_code.value
    job.error_message = error.message
    session.commit()
    raise error


async def run_strapi_sync(
    session: Session,
    job: GenerationJob,
    provider,
    *,
    settings: Settings | None = None,
) -> StrapiSyncRow:
    """Run the full two-phase Strapi Draft sync for one job."""
    settings = settings or get_settings()

    job.status = JobStatus.STRAPI_SYNCING.value
    job.current_step = "strapi_sync"
    session.flush()

    row: StrapiSyncRow | None = None
    try:
        version = latest_article_version(session, job)
        if version is None:
            raise PipelineError(
                ErrorCode.ARTICLE_VALIDATION_FAILED,
                "no final article to sync — run the article pipeline first",
            )
        brief = load_research_context(session, job)["brief"]
        doc = _rebuild_document(version, brief, job)

        # ---- pre-checks (section 6.3 / 6.4): BEFORE any Strapi HTTP ----
        author = _resolve_relation(
            job.author_document_id,
            settings.strapi_default_author_document_id,
        )
        category = _resolve_relation(
            job.category_document_id,
            settings.strapi_default_category_document_id,
        )
        if author is None and settings.strapi_author_required:
            raise PipelineError(
                ErrorCode.STRAPI_SCHEMA_MISMATCH,
                "author documentId is required (STRAPI_AUTHOR_REQUIRED=true): "
                "set job.author_document_id or STRAPI_DEFAULT_AUTHOR_DOCUMENT_ID",
            )
        if category is None and settings.strapi_category_required:
            raise PipelineError(
                ErrorCode.STRAPI_SCHEMA_MISMATCH,
                "category documentId is required (STRAPI_CATEGORY_REQUIRED=true): "
                "set job.category_document_id or STRAPI_DEFAULT_CATEGORY_DOCUMENT_ID",
            )

        # ---- idempotency anchor (section 41) ----
        row = session.scalars(
            select(StrapiSyncRow).where(StrapiSyncRow.job_id == job.id)
        ).first()
        existing_doc_id = row.strapi_document_id if row is not None else None

        # ---- slug collision check (section 42): BEFORE any write ----
        entries = await provider.find_blogs_by_slug(doc.slug)
        for entry in entries:
            if existing_doc_id and entry.document_id == existing_doc_id:
                continue  # our own draft — the update path
            _fail_pre_sync(
                session,
                job,
                row,
                PipelineError(
                    ErrorCode.STRAPI_SLUG_CONFLICT,
                    f"slug '{doc.slug}' is already used by another Strapi "
                    f"entry (documentId={entry.document_id}, "
                    f"status={entry.status}) — change the slug or cancel the "
                    "sync (section 42: never auto-suffixed)",
                ),
            )

        planned = load_planned_images(session, job)
        hero = next((r for r in planned if r.role == "hero"), None)
        inlines = [
            r for r in planned if r.role == "inline" and r.insertion_marker
        ]
        if hero is None:
            raise PipelineError(
                ErrorCode.IMAGE_PLAN_INVALID,
                "no hero image planned — cannot satisfy mainImage (section 64)",
            )

        # ================= STEP A: text-only Draft (create or update) ====
        payload = build_draft_payload(
            doc,
            author_document_id=author,
            category_document_id=category,
            body=version.body_markdown,
            include_posted_at=settings.strapi_set_posted_at_on_draft,
            posted_at=(
                datetime.now(timezone.utc).date().isoformat()
                if settings.strapi_set_posted_at_on_draft
                else None
            ),
        )
        if row is None or existing_doc_id is None:
            entry = await provider.create_draft_entry(payload)
            row = StrapiSyncRow(
                job_id=job.id,
                strapi_id=entry.id,
                strapi_document_id=entry.document_id,
                sync_status=StrapiSyncStatus.IN_PROGRESS.value,
            )
            session.add(row)
            session.flush()
        else:
            entry = await provider.update_draft_entry(
                existing_doc_id, payload
            )
            row.strapi_id = entry.id
            row.strapi_document_id = entry.document_id
            row.sync_status = StrapiSyncStatus.IN_PROGRESS.value
        row.last_payload = payload
        row.last_payload_hash = payload_hash(payload)
        row.error_message = None
        session.commit()  # checkpoint (section 9): documentId is safe

        blog_id = row.strapi_id

        # ================= STEP B: Hero upload (section 38) ============
        if not (hero.strapi_media_id and hero.strapi_url):
            data = _read_image_bytes(hero, "hero")
            result = await provider.upload_hero(
                data,
                hero.filename,
                blog_numeric_id=blog_id,
                alt_text=hero.alt_text,
            )
            _record_media(hero, result, settings)
            session.commit()  # checkpoint (section 9): per image

        # ================= STEP C: inline uploads (section 39) ========
        for img in inlines:
            if img.strapi_media_id and img.strapi_url:
                continue  # already uploaded by a previous attempt
            data = _read_image_bytes(img, img.insertion_marker)
            result = await provider.upload_inline(
                data, img.filename, alt_text=img.alt_text
            )
            _record_media(img, result, settings)
            session.commit()  # checkpoint (section 9): per image

        # ================= STEP D: resolve markers (section 36) =======
        marked = insert_image_markers(
            version.body_markdown,
            [(r.insertion_marker, r.section_heading) for r in inlines],
        )
        images = {
            r.insertion_marker: (r.alt_text, r.strapi_url) for r in inlines
        }
        include_hero = None
        if not settings.strapi_frontend_renders_main_image:
            include_hero = (hero.alt_text, hero.strapi_url)
        final_body = resolve_image_markers(
            marked, images, include_hero=include_hero
        )

        # ================= STEP E: final PUT (section 40) =============
        final_payload = build_final_body_payload(final_body)
        await provider.update_draft_entry(
            row.strapi_document_id, final_payload
        )
        row.last_payload = final_payload
        row.last_payload_hash = payload_hash(final_payload)
        session.commit()

        # ================= STEP F: GET verify (section 64) ============
        fetched = await provider.get_draft(row.strapi_document_id)
        missing = [
            field
            for field, value in (
                ("title", fetched.title),
                ("slug", fetched.slug),
                ("author", fetched.author),
                ("category", fetched.category),
                ("mainImage", fetched.main_image),
                ("body", fetched.body),
                ("metaTitle", fetched.meta_title),
                ("metaDescription", fetched.meta_description),
                ("seoKeywords", fetched.seo_keywords),
            )
            if value in (None, "")
        ]
        if missing:
            raise PipelineError(
                ErrorCode.STRAPI_SCHEMA_MISMATCH,
                f"GET verify failed after sync — missing fields: "
                f"{', '.join(missing)}",
            )

        # ---- success (section 64: all 9 fields + GET + documentId) ----
        row.sync_status = StrapiSyncStatus.DRAFT_CREATED.value
        row.error_message = None
        job.status = JobStatus.STRAPI_DRAFT_CREATED.value
        job.current_step = "strapi_sync"
        job.error_code = None
        job.error_message = None
        session.commit()
        logger.info(
            "strapi_draft_created",
            extra={
                "event": "strapi_draft_created",
                "job_id": str(job.id),
                "strapi_document_id": row.strapi_document_id,
            },
        )
        return row
    except PipelineError as error:
        # Section 64: keep the documentId so the retry UPDATES the same
        # draft. Only fields that are actually safe to touch are set.
        if row is not None:
            row.sync_status = StrapiSyncStatus.FAILED.value
            row.error_message = str(error)
        job.error_code = error.error_code.value
        job.error_message = error.message
        session.commit()
        logger.warning(
            "strapi_sync_failed",
            extra={
                "event": "strapi_sync_failed",
                "job_id": str(job.id),
                "error_code": error.error_code.value,
                "strapi_document_id": (
                    row.strapi_document_id if row is not None else None
                ),
            },
        )
        raise


def _read_image_bytes(row: ImageRow, label: str) -> bytes:
    if not row.local_path:
        raise PipelineError(
            ErrorCode.STRAPI_UPLOAD_FAILED,
            f"local image file missing for {label} — run image generation first",
        )
    with open(row.local_path, "rb") as fh:
        return fh.read()


def _record_media(row: ImageRow, result, settings: Settings) -> None:
    """Fill the P7 Strapi media columns (section 46.15)."""
    row.strapi_media_id = result.media_id
    row.strapi_media_document_id = result.document_id
    row.strapi_url = resolve_media_url(result.url, settings.strapi_base_url)
