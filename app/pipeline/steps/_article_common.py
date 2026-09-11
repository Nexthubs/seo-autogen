"""Shared helpers for the P5 article pipeline steps
(SEO-AUTO-DEV-SPEC.md sections 24-29).

Versioning (section 28): every generated article is a NEW row in
``article_versions``; nothing is ever overwritten. Reviews (section 26)
and the anti-copy report (section 29) are persisted per article
version in ``article_reviews``.
"""

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.article import ArticleReviewRow, ArticleVersionRow
from app.db.models.job import GenerationJob
from app.db.models.research import (
    ArticleOutlineRow,
    ContentBriefRow,
    EvidenceNoteRow,
    SerpSynthesisRow,
)
from app.db.models.internal_link import InternalLinkRule
from app.db.models.source import JobSource, SourcePage
from app.schemas.article import ArticleDocument

#: Section 50: the guideline is part of the Writer/Reviewer context.
MAX_GUIDELINE_CHARS = 24000
REVISION_REVIEW_TYPES = ("seo", "fact", "style")


def latest_article_version(
    session: Session, job: GenerationJob
) -> ArticleVersionRow | None:
    """The highest-numbered non-invalidated article version for the job.

    This is the FINAL / shipped version: after a complete run the highest
    version is the ``revision`` stage. Use it for "what ships" consumers
    (image planning, Strapi sync, DoD, the article API).
    """
    return session.scalars(
        select(ArticleVersionRow)
        .where(
            ArticleVersionRow.job_id == job.id,
            ArticleVersionRow.invalidated_at.is_(None),
        )
        .order_by(ArticleVersionRow.version.desc())
    ).first()


def latest_writer_version(
    session: Session, job: GenerationJob
) -> ArticleVersionRow | None:
    """The current writer draft: the highest ``stage="writer"`` version.

    ``article_versions`` is immutable, append-only history (spec sections
    27-28): a retry re-runs the writer and appends a NEW draft (v1, v3, ...)
    rather than deleting the old one, and the old ``revision`` (v2) stays
    behind. The "current valid" draft is therefore DERIVED as the newest
    writer version — NOT the overall newest version, which after a retry may
    be a stale revision. The reviewers and the reviser target this draft so
    a stale revision can never shadow the new one.
    """
    return session.scalars(
        select(ArticleVersionRow)
        .where(
            ArticleVersionRow.job_id == job.id,
            ArticleVersionRow.stage == "writer",
            ArticleVersionRow.invalidated_at.is_(None),
        )
        .order_by(ArticleVersionRow.version.desc())
    ).first()


def next_article_version(session: Session, job: GenerationJob) -> int:
    # Invalidated rows remain immutable history and still own their version
    # numbers, so sequence allocation must consider every historical row.
    current = session.scalar(
        select(func.max(ArticleVersionRow.version)).where(
            ArticleVersionRow.job_id == job.id
        )
    )
    return int(current or 0) + 1


def persist_article_version(
    session: Session,
    job: GenerationJob,
    doc: ArticleDocument,
    *,
    stage: str,
    model: str | None,
    prompt_name: str | None,
    prompt_version: str | None,
    prompt_hash: str | None = None,
    based_on_reviews: dict | None = None,
) -> ArticleVersionRow:
    """Append a NEW version row (section 28: never overwrite).

    ``based_on_reviews`` (R-M03) records, per review type, the exact
    ``article_reviews`` row + attempt a ``revision`` version was produced
    from, so the revision stays traceable after later review retries.
    """
    row = ArticleVersionRow(
        job_id=job.id,
        version=next_article_version(session, job),
        stage=stage,
        title=doc.title,
        body_markdown=doc.body_markdown,
        seo_title=doc.seo_title,
        meta_description=doc.meta_description,
        slug=doc.slug,
        model=model,
        prompt_name=prompt_name,
        prompt_version=prompt_version,
        prompt_hash=prompt_hash,
        based_on_reviews=based_on_reviews,
    )
    session.add(row)
    session.flush()
    return row


def latest_review_row(
    session: Session,
    job: GenerationJob,
    version_row: ArticleVersionRow,
    review_type: str,
) -> ArticleReviewRow | None:
    """The CURRENT VALID review row: highest ``attempt`` for that
    (version, type).

    Reviews are append-only history, so "which verdict is current" is the
    highest non-invalidated attempt, never expressed by deleting older rows.
    """
    return session.scalars(
        select(ArticleReviewRow)
        .where(
            ArticleReviewRow.job_id == job.id,
            ArticleReviewRow.article_version_id == version_row.id,
            ArticleReviewRow.review_type == review_type,
            ArticleReviewRow.invalidated_at.is_(None),
        )
        .order_by(ArticleReviewRow.attempt.desc())
    ).first()


def persist_review(
    session: Session,
    job: GenerationJob,
    version_row: ArticleVersionRow,
    *,
    review_type: str,
    review: dict,
    model: str | None = None,
    prompt_version: str | None = None,
    prompt_hash: str | None = None,
) -> ArticleReviewRow:
    """Append one review run for one article version (section 46.14).

    R-M03: re-running a step no longer deletes the previous verdict for the
    (version, type) pair — it appends a new attempt. The current valid
    verdict is the highest ``attempt`` (:func:`latest_review_row`). This
    keeps every review run that an already-persisted revision was built
    from traceable.
    """
    last_attempt = session.scalars(
        select(ArticleReviewRow.attempt)
        .where(
            ArticleReviewRow.job_id == job.id,
            ArticleReviewRow.article_version_id == version_row.id,
            ArticleReviewRow.review_type == review_type,
        )
        .order_by(ArticleReviewRow.attempt.desc())
    ).first()
    row = ArticleReviewRow(
        job_id=job.id,
        article_version_id=version_row.id,
        review_type=review_type,
        review=review,
        model=model,
        prompt_version=prompt_version,
        prompt_hash=prompt_hash,
        attempt=(last_attempt or 0) + 1,
    )
    session.add(row)
    session.flush()
    return row


def review_lineage(
    session: Session,
    job: GenerationJob,
    version_row: ArticleVersionRow,
) -> dict[str, dict]:
    """The current review attempt set for a version, keyed by review type.

    Shape: ``{"seo": {"review_id": "<uuid>", "attempt": 1}, ...}`` — the
    value persisted on a revision's ``based_on_reviews`` (R-M03).
    """
    rows = session.scalars(
        select(ArticleReviewRow)
        .where(
            ArticleReviewRow.job_id == job.id,
            ArticleReviewRow.article_version_id == version_row.id,
            ArticleReviewRow.invalidated_at.is_(None),
        )
        .order_by(ArticleReviewRow.attempt.desc())
    ).all()
    lineage: dict[str, dict] = {}
    for row in rows:
        # Rows are ordered by attempt desc, so the first per type is current.
        if row.review_type not in lineage:
            lineage[row.review_type] = {
                "review_id": str(row.id),
                "attempt": row.attempt,
            }
    return lineage


def revision_matches_current_reviews(
    session: Session,
    job: GenerationJob,
    writer: ArticleVersionRow,
    revision: ArticleVersionRow,
) -> bool:
    """Whether ``revision`` consumed the current valid reviewer attempts.

    A later retry can append a new review attempt for the same writer while
    retaining the old revision for audit.  Such a revision is no longer a
    valid checkpoint or shippable final even though its version is newer.
    """
    if revision.stage != "revision" or revision.invalidated_at is not None:
        return False
    expected: dict[str, dict] = {}
    for review_type in REVISION_REVIEW_TYPES:
        row = latest_review_row(session, job, writer, review_type)
        if row is None:
            return False
        expected[review_type] = {
            "review_id": str(row.id),
            "attempt": row.attempt,
        }
    actual = revision.based_on_reviews or {}
    return actual == expected


def latest_review(
    session: Session,
    job: GenerationJob,
    version_row: ArticleVersionRow,
    review_type: str,
) -> dict | None:
    row = latest_review_row(session, job, version_row, review_type)
    return row.review if row is not None else None


def load_research_context(session: Session, job: GenerationJob) -> dict:
    """The Writer's full input context (section 50, section 25).

    Never includes full competitor texts (they go to the
    CompetitorAnalyzer only, section 16/25).
    """
    brief_row = session.scalars(
        select(ContentBriefRow).where(ContentBriefRow.job_id == job.id)
    ).first()
    outline_row = session.scalars(
        select(ArticleOutlineRow).where(ArticleOutlineRow.job_id == job.id)
    ).first()
    synthesis_row = session.scalars(
        select(SerpSynthesisRow).where(SerpSynthesisRow.job_id == job.id)
    ).first()
    evidence = [
        {
            "claim": n.claim,
            "source_title": n.source_title,
            "source_url": n.source_url,
            "source_type": n.source_type,
            "confidence": n.confidence,
            "usage": n.usage,
            "note": n.note,
            # R-H06: the source-support verdict + corroborating excerpt travel
            # with the note so the writer / fact reviewer can see what the
            # fetched source actually says.
            "verification_status": n.verification_status,
            "supporting_excerpt": n.supporting_excerpt,
        }
        for n in session.scalars(
            select(EvidenceNoteRow).where(EvidenceNoteRow.job_id == job.id)
        ).all()
    ]
    markers = list(
        session.scalars(
            select(InternalLinkRule.marker).where(
                InternalLinkRule.active.is_(True)
            )
        ).all()
    )
    return {
        "brief": brief_row.brief if brief_row is not None else None,
        "outline": outline_row.outline if outline_row is not None else None,
        "synthesis": synthesis_row.synthesis
        if synthesis_row is not None
        else {},
        "evidence": evidence,
        "markers": markers,
    }


def load_competitor_texts(session: Session, job: GenerationJob) -> list[tuple[str, str]]:
    """Full competitor texts, ranked — ONLY for the programmatic
    anti-copy check (section 29). Never sent to any LLM stage.
    """
    pairs: list[tuple[str, str]] = []
    for rank, page in enumerate(
        session.scalars(
            select(SourcePage)
            .join(JobSource, JobSource.source_page_id == SourcePage.id)
            .where(JobSource.job_id == job.id)
            .order_by(JobSource.serp_rank)
        ).all()
    ):
        del rank
        pairs.append((page.url, page.content_markdown or ""))
    return pairs


def build_article_view(row: ArticleVersionRow, keyword: str | None = None) -> dict:
    """The article as a plain dict for prompts."""
    view = {
        "title": row.title,
        "body_markdown": row.body_markdown,
        "seo_title": row.seo_title,
        "meta_description": row.meta_description,
        "slug": row.slug,
    }
    if keyword is not None:
        view["primary_keyword"] = keyword
    return view


def article_view_json(row: ArticleVersionRow) -> str:
    return json.dumps(build_article_view(row), ensure_ascii=False, indent=1)
