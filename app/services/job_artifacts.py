"""Complete local artifact export (SEO-AUTO-DEV-SPEC.md section 34, audit L01).

Section 34 lists the full per-job directory::

    data/articles/{job_uuid}/
        article.md            (written by the image step / renderer)
        article.json          (written by the image step / renderer)
        content-brief.json    <- this module (L01)
        outline.json          <- this module (L01)
        serp.json             <- this module (L01)
        review.json           <- this module (L01)
        sources.json          <- this module (L01)
        images/...

Before L01 only ``article.md``/``article.json`` and the images were written,
so the local export was NOT a self-contained offline delivery: the research
artifacts (brief, outline, SERP, reviews, sources) lived only in the DB.
``export_research_artifacts`` closes that gap so the directory on disk is a
complete, auditable snapshot of one article.

The export is a READ of the DB rows — it never mutates them, and it is
defensive: any row group that is absent (e.g. a partial run, or the unit
tests that only seed article + image rows) simply yields an empty list for
that group instead of raising.
"""

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.article import ArticleReviewRow
from app.db.models.research import (
    ArticleOutlineRow,
    ContentBriefRow,
    EvidenceNoteRow,
    SerpSynthesisRow,
)
from app.db.models.serp import SerpResult, SerpRun
from app.db.models.source import JobSource, SourcePage
from app.db.models.job import GenerationJob


def _dump(path: Path, payload) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8",
    )


def _content_brief(session: Session, job: GenerationJob) -> dict | None:
    row = session.scalar(
        select(ContentBriefRow).where(ContentBriefRow.job_id == job.id)
    )
    return row.brief if row else None


def _outline(session: Session, job: GenerationJob) -> dict | None:
    row = session.scalar(
        select(ArticleOutlineRow).where(ArticleOutlineRow.job_id == job.id)
    )
    if not row:
        return None
    return {
        "outline": row.outline,
        "valid": row.valid,
        "repair_count": row.repair_count,
        "model": row.model,
        "prompt_version": row.prompt_version,
    }


def _serp(session: Session, job: GenerationJob) -> dict:
    runs = session.scalars(
        select(SerpRun).where(SerpRun.job_id == job.id).order_by(SerpRun.created_at)
    ).all()
    run_ids = [r.id for r in runs]
    results = (
        session.scalars(
            select(SerpResult).where(SerpResult.serp_run_id.in_(run_ids))
        ).all()
        if run_ids
        else []
    )
    by_run: dict = {}
    for res in results:
        by_run.setdefault(res.serp_run_id, []).append(
            {
                "result_type": res.result_type,
                "rank": res.rank,
                "title": res.title,
                "url": res.url,
                "normalized_url": res.normalized_url,
                "domain": res.domain,
                "snippet": res.snippet,
                "raw_item": res.raw_item,
            }
        )
    synthesis = session.scalar(
        select(SerpSynthesisRow).where(SerpSynthesisRow.job_id == job.id)
    )
    return {
        "runs": [
            {
                "provider": run.provider,
                "query": run.query,
                "location_code": run.location_code,
                "language_code": run.language_code,
                "device": run.device,
                "provider_cost": float(run.provider_cost)
                if run.provider_cost is not None
                else None,
                "results": by_run.get(run.id, []),
            }
            for run in runs
        ],
        "synthesis": synthesis.synthesis if synthesis else None,
    }


def _reviews(session: Session, job: GenerationJob) -> list[dict]:
    rows = session.scalars(
        select(ArticleReviewRow)
        .where(ArticleReviewRow.job_id == job.id)
        .order_by(ArticleReviewRow.created_at)
    ).all()
    return [
        {
            "article_version_id": str(r.article_version_id),
            "review_type": r.review_type,
            "review": r.review,
            "model": r.model,
            "prompt_version": r.prompt_version,
            "created_at": r.created_at,
        }
        for r in rows
    ]


def _sources(session: Session, job: GenerationJob) -> dict:
    links = session.scalars(
        select(JobSource).where(JobSource.job_id == job.id)
    ).all()
    page_ids = [l.source_page_id for l in links]
    pages = (
        session.scalars(
            select(SourcePage).where(SourcePage.id.in_(page_ids))
        ).all()
        if page_ids
        else []
    )
    page_by_id = {p.id: p for p in pages}
    evidence = session.scalars(
        select(EvidenceNoteRow).where(EvidenceNoteRow.job_id == job.id)
    ).all()
    return {
        "sources": [
            {
                "serp_rank": l.serp_rank,
                "source_role": l.source_role,
                "url": page_by_id[l.source_page_id].url
                if l.source_page_id in page_by_id
                else None,
                "title": page_by_id[l.source_page_id].title
                if l.source_page_id in page_by_id
                else None,
            }
            for l in links
        ],
        "evidence_notes": [
            {
                "claim": e.claim,
                "source_title": e.source_title,
                "source_url": e.source_url,
                "source_type": e.source_type,
                "confidence": e.confidence,
                "usage": e.usage,
                "note": e.note,
                "verification_status": e.verification_status,
                "supporting_excerpt": e.supporting_excerpt,
            }
            for e in evidence
        ],
    }


def export_research_artifacts(session: Session, job: GenerationJob, job_dir: Path) -> list[str]:
    """Write the section-34 research JSONs into ``job_dir``.

    Returns the list of filenames written. Missing row groups produce an
    empty/None payload (the file is still written so the directory layout
    is complete and the absence of data is explicit, not implicit).
    """
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []

    brief = _content_brief(session, job)
    _dump(job_dir / "content-brief.json", brief if brief is not None else {})
    written.append("content-brief.json")

    outline = _outline(session, job)
    _dump(job_dir / "outline.json", outline if outline is not None else {})
    written.append("outline.json")

    _dump(job_dir / "serp.json", _serp(session, job))
    written.append("serp.json")

    _dump(job_dir / "review.json", {"reviews": _reviews(session, job)})
    written.append("review.json")

    _dump(job_dir / "sources.json", _sources(session, job))
    written.append("sources.json")

    return written
