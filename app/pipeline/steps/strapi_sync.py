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
    build_seo_keywords,
    media_id,
    media_url,
    payload_hash,
    relation_document_id,
    resolve_media_url,
)
from app.services.final_body import render_final_body

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
    """Persist the unified failed state (M01, section 64).

    Even a pre-HTTP failure (slug conflict, required author/category
    missing) must land in a UNIFIED persisted state:

    * a ``strapi_syncs`` row exists with ``sync_status=failed`` and
      the error — so the UI/API can always show and retry it, and a
      later mid-sync retry knows exactly where to resume;
    * the job moves to ``strapi_sync_failed`` (section 64: the
      article pipeline already succeeded — only the sync failed, so
      ``failed`` is NOT the right status).
    """
    if row is None:
        row = StrapiSyncRow(
            job_id=job.id,
            strapi_id=None,
            strapi_document_id=None,
            sync_status=StrapiSyncStatus.FAILED.value,
        )
        session.add(row)
    row.sync_status = StrapiSyncStatus.FAILED.value
    row.error_message = str(error)
    job.status = JobStatus.STRAPI_SYNC_FAILED.value
    job.current_step = "strapi_sync"
    job.error_code = error.error_code.value
    job.error_message = error.message
    job.completed_at = job.completed_at or datetime.now(timezone.utc)
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

        # ================= STEP D: resolve markers (section 36, H07) ===
        # Shared final renderer (sections 21 + 33): internal link markers
        # resolve FIRST, image markers second, and a hard assertion that
        # no raw marker reaches Strapi (the body PUT next).
        images = {r.insertion_marker: (r.alt_text, r.strapi_url) for r in inlines}
        include_hero = None
        if not settings.strapi_frontend_renders_main_image:
            include_hero = (hero.alt_text, hero.strapi_url)
        final_body = render_final_body(
            session,
            version.body_markdown,
            images=images,
            placements=[(r.insertion_marker, r.section_heading) for r in inlines],
            include_hero=include_hero,
        )

        # ================= STEP E: final PUT (section 40) =============
        final_payload = build_final_body_payload(final_body)
        await provider.update_draft_entry(
            row.strapi_document_id, final_payload
        )
        row.last_payload = final_payload
        row.last_payload_hash = payload_hash(final_payload)
        session.commit()

        # ================= STEP F: GET verify (section 64, M02) =====
        # Verify the NORMALIZED values — not just non-empty:
        #   * id / documentId anchor the retry;
        #   * status must stay "draft" (we never publish, and a
        #     "published" read-back means someone/something published
        #     it — fail, don't report success);
        #   * title/slug/body/metaTitle/metaDescription/seoKeywords
        #     must equal what WE pushed;
        #   * author/category resolve (short documentId string,
        #     numeric id, or populated object) to the expected
        #     documentIds;
        #   * mainImage matches the uploaded hero media.
        fetched = await provider.get_draft(row.strapi_document_id)

        problems: list[str] = []
        if fetched.id != row.strapi_id:
            problems.append(
                f"id mismatch: got {fetched.id!r}, expected {row.strapi_id!r}"
            )
        if fetched.document_id != row.strapi_document_id:
            problems.append(
                f"documentId mismatch: got {fetched.document_id!r}, "
                f"expected {row.strapi_document_id!r}"
            )
        if fetched.status != "draft":
            problems.append(
                f"status is {fetched.status!r} — expected 'draft' "
                "(this tool never publishes)"
            )
        if (fetched.title or "") != doc.title:
            problems.append(
                f"title mismatch: got {fetched.title!r}, expected {doc.title!r}"
            )
        if (fetched.slug or "") != doc.slug:
            problems.append(
                f"slug mismatch: got {fetched.slug!r}, expected {doc.slug!r}"
            )
        if (fetched.body or "") != final_body:
            problems.append(
                "body mismatch: stored body does not equal the final "
                "rendered body that was PUT"
            )
        if (fetched.meta_title or "") != doc.seo_title:
            problems.append(
                f"metaTitle mismatch: got {fetched.meta_title!r}, "
                f"expected {doc.seo_title!r}"
            )
        if (fetched.meta_description or "") != doc.meta_description:
            problems.append(
                "metaDescription mismatch: got "
                f"{fetched.meta_description!r}, expected "
                f"{doc.meta_description!r}"
            )
        expected_keywords = build_seo_keywords(
            doc.primary_keyword,
            doc.secondary_keywords,
            doc.long_tail_keywords,
        )
        if (fetched.seo_keywords or "") != expected_keywords:
            problems.append(
                "seoKeywords mismatch: got "
                f"{fetched.seo_keywords!r}, expected {expected_keywords!r}"
            )
        expected_author = relation_document_id(
            job.author_document_id or settings.strapi_default_author_document_id
        )
        if expected_author is not None:
            actual_author = relation_document_id(fetched.author)
            if actual_author != expected_author:
                problems.append(
                    f"author mismatch: got {actual_author!r}, "
                    f"expected {expected_author!r}"
                )
        expected_category = relation_document_id(
            job.category_document_id
            or settings.strapi_default_category_document_id
        )
        if expected_category is not None:
            actual_category = relation_document_id(fetched.category)
            if actual_category != expected_category:
                problems.append(
                    f"category mismatch: got {actual_category!r}, "
                    f"expected {expected_category!r}"
                )
        # Compare by URL PATH (section 39): we stored the absolute
        # public URL, Strapi stores the raw one (possibly relative) —
        # the path is what identifies the media file on disk.
        expected_media_url = media_url(hero.strapi_url)
        if expected_media_url:
            actual_media_url = media_url(fetched.main_image)
            if _url_path(expected_media_url) != _url_path(
                actual_media_url or ""
            ):
                problems.append(
                    f"mainImage mismatch: got {actual_media_url!r}, "
                    f"expected {expected_media_url!r}"
                )
        # media id match when Strapi exposes it (populated object)
        if hero.strapi_media_id is not None:
            actual_media_id = media_id(fetched.main_image)
            if (
                actual_media_id is not None
                and actual_media_id != int(hero.strapi_media_id)
            ):
                problems.append(
                    f"mainImage media id mismatch: got {actual_media_id}, "
                    f"expected {hero.strapi_media_id}"
                )
        if problems:
            raise PipelineError(
                ErrorCode.STRAPI_SCHEMA_MISMATCH,
                f"GET verify failed after sync: {'; '.join(problems)}",
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
        # Section 64 + M01: a mid-sync failure keeps the documentId so
        # the retry UPDATES the same draft. The persisted state is
        # UNIFIED regardless of where it failed:
        #   * the sync row is always failed (+ error message);
        #   * the job is always ``strapi_sync_failed`` (the pipeline
        #     itself succeeded — retrying the full pipeline must stay
        #     409, only the sync is retried).
        # ``_fail_pre_sync`` may already have created + committed a row
        # while the LOCAL ``row`` here is still None (the idempotency
        # anchor select never ran) — re-query before inserting so we
        # never double-create (UNIQUE on job_id).
        if row is None:
            row = session.scalars(
                select(StrapiSyncRow).where(StrapiSyncRow.job_id == job.id)
            ).first()
            if row is None:
                # First-precheck failure (author/category missing, no
                # final article): no row existed yet — still persist
                # one so the failure is visible AND retryable (M01).
                row = StrapiSyncRow(
                    job_id=job.id,
                    strapi_id=None,
                    strapi_document_id=None,
                    sync_status=StrapiSyncStatus.FAILED.value,
                )
                session.add(row)
        row.sync_status = StrapiSyncStatus.FAILED.value
        row.error_message = str(error)
        job.status = JobStatus.STRAPI_SYNC_FAILED.value
        job.current_step = "strapi_sync"
        job.error_code = error.error_code.value
        job.error_message = error.message
        job.completed_at = job.completed_at or datetime.now(timezone.utc)
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


def _url_path(url: str) -> str:
    """Path component of a media URL for comparison (M02).

    Strapi returns relative ``/uploads/...`` URLs; we persist the
    absolute public URL (section 39). Comparing by path keeps the
    verification correct either way.
    """
    if not url:
        return ""
    path = url.split("?", 1)[0].split("#", 1)[0]
    if "://" in path:
        path = path.split("://", 1)[1]
        path = path.split("/", 1)[1] if "/" in path else ""
    if not path.startswith("/"):
        path = "/" + path
    return path or "/"


def _read_image_bytes(row: ImageRow, label: str) -> bytes:
    if not row.local_path:
        raise PipelineError(
            ErrorCode.STRAPI_UPLOAD_FAILED,
            f"local image file missing for {label} — run image generation first",
        )
    try:
        with open(row.local_path, "rb") as fh:
            return fh.read()
    except OSError as error:  # M01: path set but file vanished (cleanup, disk)
        raise PipelineError(
            ErrorCode.STRAPI_UPLOAD_FAILED,
            f"local image file unreadable for {label}: {error}",
        ) from error


def _record_media(row: ImageRow, result, settings: Settings) -> None:
    """Fill the P7 Strapi media columns (section 46.15)."""
    row.strapi_media_id = result.media_id
    row.strapi_media_document_id = result.document_id
    row.strapi_url = resolve_media_url(result.url, settings.strapi_base_url)
