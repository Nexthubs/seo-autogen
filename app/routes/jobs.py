"""Job routes — REST API (spec 44/45) + job-detail data (spec 43.2/43.3).

The REST surface here is exactly the spec's ``/api/jobs*`` list. Job creation
funnels both the web form (43.1) and this endpoint through
:class:`~app.schemas.job.CreateJobRequest`; the pipeline itself is enqueued
to RQ so the work outlives the browser (43.1).

``retry`` / ``cancel`` are implemented at the minimal P8 boundary: retry
re-queues a terminal job for a fresh full run (per-step retry/resume is P9)
and cancel flips a non-terminal job to ``cancelled`` (in-flight cancellation
is P9).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.enums import JobStatus
from app.db.models.article import ArticleReviewRow, ArticleVersionRow
from app.db.models.images import ImageRow
from app.db.models.job import GenerationJob
from app.db.models.research import (
    ArticleOutlineRow,
    CompetitorAnalysisRow,
    ContentBriefRow,
    EvidenceNoteRow,
    SerpSynthesisRow,
)
from app.db.models.serp import SerpResult, SerpRun
from app.db.models.source import JobSource, SourcePage
from app.db.models.strapi_syncs import StrapiSyncRow
from app.db.session import get_db
from app.pipeline.steps._article_common import latest_article_version
from app.schemas.job import (
    DEFAULT_LANGUAGE,
    DEFAULT_MARKET,
    CreateJobRequest,
    CreateJobResponse,
)
from app.pipeline import checkpoints
from app.services.error_catalog import explain_error
from app.services.internal_link_service import resolve_markers, validate_markers
from app.services.keyword_service import lookup_metrics

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

#: Processing statuses in checkpoint order (spec section 9) — 14 stages;
#: ``ready`` is stage 15.
_PIPELINE_ORDER: tuple[str, ...] = (
    "keyword_preparing",
    "serp_searching",
    "source_extracting",
    "serp_analyzing",
    "evidence_researching",
    "brief_generating",
    "outline_generating",
    "article_generating",
    "seo_reviewing",
    "fact_reviewing",
    "style_reviewing",
    "article_revising",
    "image_planning",
    "image_generating",
)
_TOTAL_STAGES = len(_PIPELINE_ORDER) + 1  # +ready


def _fmt_dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "—"


def job_progress(job: GenerationJob) -> dict:
    """1..15 pipeline stage for a job (used by the /jobs table + detail)."""
    status = job.status
    if status == "queued":
        return {"stage": 0, "total": _TOTAL_STAGES, "pct": 0, "label": "Queued"}
    if status in ("ready", "strapi_syncing", "strapi_draft_created"):
        return {"stage": _TOTAL_STAGES, "total": _TOTAL_STAGES, "pct": 100, "label": "Ready"}
    if status in ("failed", "cancelled"):
        return {
            "stage": 0,
            "total": _TOTAL_STAGES,
            "pct": 0,
            "label": "Failed" if status == "failed" else "Cancelled",
        }
    try:
        idx = _PIPELINE_ORDER.index(status) + 1
    except ValueError:
        idx = 0
    return {
        "stage": idx,
        "total": _TOTAL_STAGES,
        "pct": int(idx / _TOTAL_STAGES * 100),
        "label": status.replace("_", " ").title(),
    }


def job_model_label(session: Session, job: GenerationJob) -> str:
    """The LLM model that produced the latest article version (43.2)."""
    row = latest_article_version(session, job)
    if row is not None and (row.model or row.prompt_name):
        return row.model or row.prompt_name or "—"
    return "—"


# ----------------------------------------------------------------------
# Job creation (spec 45)
# ----------------------------------------------------------------------
def _enqueue(job_id: uuid.UUID, options: dict | None = None) -> bool:
    """Best-effort enqueue; a down Redis must not break job creation.

    When there are no run options the call falls back to the single-argument
    ``enqueue_job(job_id)`` form (the P8 contract), so callers that patch a
    single-arg ``enqueue_job`` keep working. P9-A run options are passed as a
    keyword-only second argument.
    """
    from app.workers import article_tasks

    try:
        if options:
            article_tasks.enqueue_job(job_id, options=options)
        else:
            article_tasks.enqueue_job(job_id)
        return True
    except Exception:  # noqa: BLE001 - degrade to manual retry (43.3)
        return False


def _reset_artifacts(session: Session, job: GenerationJob) -> None:
    """Clear per-run artifacts so a full-pipeline retry is clean.

    Most steps are replace-on-rerun, but ``competitor_analysis`` appends and
    ``serp_search`` adds a new run, so we delete the previous run's outputs
    before re-running. Per-step cleanup granularity is P9.
    """
    session.execute(delete(SerpRun).where(SerpRun.job_id == job.id))
    session.execute(delete(JobSource).where(JobSource.job_id == job.id))
    session.execute(
        delete(CompetitorAnalysisRow).where(CompetitorAnalysisRow.job_id == job.id)
    )
    session.execute(
        delete(SerpSynthesisRow).where(SerpSynthesisRow.job_id == job.id)
    )
    session.execute(
        delete(EvidenceNoteRow).where(EvidenceNoteRow.job_id == job.id)
    )
    session.execute(delete(ContentBriefRow).where(ContentBriefRow.job_id == job.id))
    session.execute(
        delete(ArticleOutlineRow).where(ArticleOutlineRow.job_id == job.id)
    )
    session.execute(delete(ArticleReviewRow).where(ArticleReviewRow.job_id == job.id))
    session.execute(
        delete(ArticleVersionRow).where(ArticleVersionRow.job_id == job.id)
    )
    session.execute(delete(ImageRow).where(ImageRow.job_id == job.id))
    session.commit()


def create_job(session: Session, payload: CreateJobRequest) -> tuple[GenerationJob, bool]:
    """Persist a queued job and enqueue its pipeline run."""
    job = GenerationJob(
        keyword=payload.keyword,
        language=payload.language or DEFAULT_LANGUAGE,
        market=payload.market or DEFAULT_MARKET,
        target_function=payload.target_function,
        strategy=payload.strategy,
        author_document_id=payload.author_document_id,
        category_document_id=payload.category_document_id,
        image_count_override=payload.effective_image_override(),
        status=JobStatus.QUEUED.value,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    enqueued = _enqueue(job.id)
    return job, enqueued


def _get_job_or_404(session: Session, job_id: uuid.UUID) -> GenerationJob:
    job = session.get(GenerationJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


def _parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="job not found")


# ----------------------------------------------------------------------
# REST: collection
# ----------------------------------------------------------------------
@router.post("", response_model=CreateJobResponse)
def api_create_job(
    payload: CreateJobRequest, session: Session = Depends(get_db)
) -> CreateJobResponse:
    job, _ = create_job(session, payload)
    return CreateJobResponse(job_id=str(job.id), status=job.status)


@router.get("")
def api_list_jobs(session: Session = Depends(get_db)) -> dict:
    jobs = session.scalars(
        select(GenerationJob)
        .order_by(GenerationJob.created_at.desc())
        .limit(200)
    ).all()
    return {
        "jobs": [
            {
                "job_id": str(j.id),
                "keyword": j.keyword,
                "status": j.status,
                "progress": job_progress(j),
                "created_at": _fmt_dt(j.created_at),
            }
            for j in jobs
        ]
    }


# ----------------------------------------------------------------------
# REST: single job + artifacts
# ----------------------------------------------------------------------
def _latest_serp(session: Session, job: GenerationJob) -> SerpRun | None:
    return (
        session.scalars(
            select(SerpRun)
            .where(SerpRun.job_id == job.id)
            .order_by(SerpRun.created_at.desc())
        )
        .first()
    )


@router.get("/{job_id}")
def api_get_job(job_id: str, session: Session = Depends(get_db)) -> dict:
    job = _get_job_or_404(session, _parse_uuid(job_id))
    return {
        "job_id": str(job.id),
        "keyword": job.keyword,
        "language": job.language,
        "market": job.market,
        "strategy": job.strategy,
        "status": job.status,
        "current_step": job.current_step,
        "progress": job_progress(job),
        "error": _error_payload(session, job),
        "created_at": _fmt_dt(job.created_at),
        "started_at": _fmt_dt(job.started_at),
        "completed_at": _fmt_dt(job.completed_at),
    }


def _error_payload(session: Session, job: GenerationJob) -> dict | None:
    """Error block with the Chinese cause/advice (P9-B3).

    ``last_failed_step`` is *derived at read time* from the checkpoint rows
    — the first step whose checkpoint output was never committed — rather
    than stored on the job row, so it stays correct for every failure without
    a schema change. The retry button in the panel sends this step to the
    P9-A ``retry_step`` endpoint.
    """
    if job.status != "failed" or not (job.error_code or job.error_message):
        return None
    last_failed_step = checkpoints.first_incomplete_step(session, job)
    step_label = (
        checkpoints.STEP_LABELS[checkpoints.STEP_NAMES[last_failed_step - 1]]
        if last_failed_step
        else None
    )
    payload = explain_error(
        job.error_code,
        job.error_message,
        last_failed_step=last_failed_step,
        step_label=step_label,
    )
    # Spec section 49 / audit M11: expose the raw failure detail (model raw
    # output or exception traceback) for debugging; null when absent.
    payload["error_raw"] = job.error_raw
    return payload


def _parse_retry_options(data: dict) -> tuple[str, int | None, bool]:
    """Validate retry request fields -> (mode, step, force_source_refresh).

    ``mode`` is one of ``full`` (default), ``step`` (Retry by step) or
    ``resume`` (Resume from checkpoint). ``step`` is the 1-based step index
    for mode ``step``. ``force_source_refresh`` is a bool (spec 14.2).
    """
    mode = str(data.get("mode") or "full").lower()
    if mode not in ("full", "step", "resume"):
        raise HTTPException(status_code=400, detail=f"unknown retry mode: {mode}")
    force = str(data.get("force_source_refresh") or "").lower() in ("1", "true", "on", "yes")
    step: int | None = None
    if mode == "step":
        raw = data.get("step")
        try:
            step = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="step must be an integer")
        if not 1 <= step <= 15:
            raise HTTPException(status_code=400, detail="step must be 1..15")
    return mode, step, force


@router.post("/{job_id}/retry")
async def api_retry_job(
    job_id: str, request: Request, session: Session = Depends(get_db)
) -> dict:
    """Re-queue a terminal job (spec 44).

    P9-A retry modes (all require a terminal job):

    - ``full`` (default): clear per-run artifacts and run the whole chain —
      the P8 minimal retry.
    - ``step`` + ``step`` (1..15): Retry by step — delete that step's
      checkpoint outputs and every later one, then re-run from it (spec 9/44).
    - ``resume``: Resume from checkpoint — skip done checkpoints and re-run
      from the first incomplete step (spec 9); nothing is deleted.

    ``force_source_refresh`` (any mode): re-extract even fresh cache entries
    (spec 14.2 "Force Refresh Sources").
    """
    job = _get_job_or_404(session, _parse_uuid(job_id))
    if not JobStatus(job.status).is_terminal:
        raise HTTPException(status_code=409, detail="job is not in a terminal state")

    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001 - empty/invalid JSON body -> full retry
            data = {}
    else:
        form = await request.form()
        data = {key: form.get(key) for key in form.keys()}

    mode, step, force = _parse_retry_options(data)

    # Sparse dict: only carry keys that actually differ from the P8 default,
    # so a plain full retry produces {} and takes the single-arg enqueue path.
    options: dict = {}
    if mode == "step":
        options["retry_step"] = step
    elif mode == "resume":
        from app.pipeline import checkpoints

        idx = checkpoints.first_incomplete_step(session, job)
        if idx is None:
            # Every checkpoint is present (e.g. a post-pipeline failure):
            # a resume would be a no-op, so fall back to a clean full run.
            _reset_artifacts(session, job)
        else:
            options["resume_from_step"] = idx
    else:  # full (P8 behaviour)
        _reset_artifacts(session, job)
    if force:
        options["force_source_refresh"] = True

    job.status = JobStatus.QUEUED.value
    job.current_step = None
    job.error_code = None
    job.error_message = None
    job.error_raw = None
    job.started_at = None
    job.completed_at = None
    session.commit()
    enqueued = _enqueue(job.id, options=options)
    return {
        "job_id": str(job.id),
        "status": job.status,
        "enqueued": enqueued,
        "mode": mode,
        "step": step,
        "force_source_refresh": force,
        **({"resume_from_step": options.get("resume_from_step")} if mode == "resume" else {}),
    }


@router.post("/{job_id}/cancel")
def api_cancel_job(job_id: str, session: Session = Depends(get_db)) -> dict:
    """Minimal P8 cancel: flip a non-terminal job to ``cancelled``."""
    job = _get_job_or_404(session, _parse_uuid(job_id))
    if JobStatus(job.status).is_terminal:
        raise HTTPException(status_code=409, detail="job is already terminal")
    job.status = JobStatus.CANCELLED.value
    job.current_step = "cancelled"
    job.completed_at = job.completed_at or datetime.utcnow()
    session.commit()
    return {"job_id": str(job.id), "status": job.status}


@router.get("/{job_id}/article")
def api_job_article(job_id: str, session: Session = Depends(get_db)) -> dict:
    job = _get_job_or_404(session, _parse_uuid(job_id))
    row = latest_article_version(session, job)
    if row is None:
        return {"job_id": str(job.id), "article": None}
    rendered, links, validation = resolve_markers(session, row.body_markdown)
    return {
        "job_id": str(job.id),
        "article": {
            "version": row.version,
            "stage": row.stage,
            "title": row.title,
            "body_markdown": row.body_markdown,
            "seo_title": row.seo_title,
            "meta_description": row.meta_description,
            "slug": row.slug,
            "rendered_markdown": rendered,
            "internal_links": [
                {"marker": l.marker, "anchor": l.anchor_text, "target": l.target_url}
                for l in links
            ],
        },
        "links_valid": validation.valid,
    }


@router.get("/{job_id}/serp")
def api_job_serp(job_id: str, session: Session = Depends(get_db)) -> dict:
    job = _get_job_or_404(session, _parse_uuid(job_id))
    run = _latest_serp(session, job)
    if run is None:
        return {"job_id": str(job.id), "serp": None}
    results = run.results
    return {
        "job_id": str(job.id),
        "serp": {
            "provider": run.provider,
            "query": run.query,
            "results": [
                {"rank": r.rank, "title": r.title, "url": r.url, "snippet": r.snippet}
                for r in sorted(results, key=lambda r: (r.rank is None, r.rank or 0))
            ],
        },
    }


@router.get("/{job_id}/sources")
def api_job_sources(job_id: str, session: Session = Depends(get_db)) -> dict:
    job = _get_job_or_404(session, _parse_uuid(job_id))
    rows = (
        session.scalars(
            select(JobSource).where(JobSource.job_id == job.id).order_by(JobSource.serp_rank)
        )
        .all()
    )
    out = []
    for js in rows:
        page = session.get(SourcePage, js.source_page_id)
        if page is None:
            continue
        out.append(
            {
                "serp_rank": js.serp_rank,
                "source_role": js.source_role,
                "url": page.url,
                "domain": page.domain,
                "title": page.title,
                "extractor": page.extractor,
                "word_count": len(page.content_markdown.split()),
            }
        )
    return {"job_id": str(job.id), "sources": out}


@router.get("/{job_id}/reviews")
def api_job_reviews(job_id: str, session: Session = Depends(get_db)) -> dict:
    job = _get_job_or_404(session, _parse_uuid(job_id))
    rows = session.scalars(
        select(ArticleReviewRow).where(ArticleReviewRow.job_id == job.id)
    ).all()
    return {
        "job_id": str(job.id),
        "reviews": [
            {"review_type": r.review_type, "review": r.review} for r in rows
        ],
    }


@router.post("/{job_id}/sync-strapi")
def api_sync_strapi(job_id: str, session: Session = Depends(get_db)) -> dict:
    """Enqueue the on-demand Strapi draft push (43.5). Never awaited here."""
    from app.workers import article_tasks

    job = _get_job_or_404(session, _parse_uuid(job_id))
    try:
        article_tasks.enqueue_strapi_sync(job.id)
        enqueued = True
    except Exception:  # noqa: BLE001
        enqueued = False
    return {"job_id": str(job.id), "enqueued": enqueued}


# ----------------------------------------------------------------------
# Detail-page payload (shared by the full page + the HTMX partial)
# ----------------------------------------------------------------------
def job_detail_payload(session: Session, job: GenerationJob) -> dict:
    """Assemble every 43.3 section for the job detail page."""
    version = latest_article_version(session, job)
    run = _latest_serp(session, job)
    metrics = lookup_metrics(session, job.keyword)
    sync = session.scalar(
        select(StrapiSyncRow).where(StrapiSyncRow.job_id == job.id).limit(1)
    )

    sources = []
    for js in (
        session.scalars(
            select(JobSource)
            .where(JobSource.job_id == job.id)
            .order_by(JobSource.serp_rank)
        )
        .all()
    ):
        page = session.get(SourcePage, js.source_page_id)
        if page is None:
            continue
        sources.append(
            {
                "serp_rank": js.serp_rank,
                "url": page.url,
                "domain": page.domain,
                "title": page.title,
                "word_count": len(page.content_markdown.split()),
            }
        )

    images = session.scalars(
        select(ImageRow).where(ImageRow.job_id == job.id).order_by(ImageRow.sort_order)
    ).all()

    reviews = session.scalars(
        select(ArticleReviewRow).where(ArticleReviewRow.job_id == job.id)
    ).all()

    # Retry / resume only applies to jobs that ended in a failure or a
    # cancellation. A ``ready`` job (or one already pushed to Strapi) is a
    # *successful* terminal state — showing "Retry" there would contradict
    # the P8 contract that a ready fragment carries no Retry action.
    retryable = JobStatus(job.status) in (
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    )
    return {
        "job": job,
        "progress": job_progress(job),
        "checkpoints": (
            checkpoints.checkpoint_status(session, job) if retryable else None
        ),
        "resume_from_step": (
            checkpoints.first_incomplete_step(session, job) if retryable else None
        ),
        "retry_available": retryable,
        "model": job_model_label(session, job),
        "metrics": metrics,
        "serp": (
            {
                "provider": run.provider,
                "query": run.query,
                "results": [
                    {"rank": r.rank, "title": r.title, "url": r.url, "snippet": r.snippet}
                    for r in sorted(
                        run.results, key=lambda r: (r.rank is None, r.rank or 0)
                    )
                ],
            }
            if run
            else None
        ),
        "sources": sources,
        "brief": _scalar_brief(session, job),
        "outline": _scalar_outline(session, job),
        "article": (
            {
                "version": version.version,
                "stage": version.stage,
                "title": version.title,
                "body_markdown": version.body_markdown,
                "seo_title": version.seo_title,
                "meta_description": version.meta_description,
                "slug": version.slug,
            }
            if version
            else None
        ),
        "reviews": [
            {"review_type": r.review_type, "review": r.review} for r in reviews
        ],
        "images": [
            {
                "role": im.role,
                "filename": im.filename,
                "alt_text": im.alt_text,
                "provider": im.provider,
                "local_path": im.local_path,
                "aspect_ratio": im.aspect_ratio,
            }
            for im in images
        ],
        "strapi": _strapi_payload(session, job, sync),
        "error": _error_payload(session, job),
        "created_at": _fmt_dt(job.created_at),
        "started_at": _fmt_dt(job.started_at),
        "completed_at": _fmt_dt(job.completed_at),
    }


def _scalar_brief(session: Session, job: GenerationJob) -> dict | None:
    row = session.scalar(
        select(ContentBriefRow).where(ContentBriefRow.job_id == job.id).limit(1)
    )
    return row.brief if row else None


def _scalar_outline(session: Session, job: GenerationJob) -> dict | None:
    row = session.scalar(
        select(ArticleOutlineRow).where(ArticleOutlineRow.job_id == job.id).limit(1)
    )
    return row.outline if row else None


def _strapi_payload(session: Session, job: GenerationJob, sync) -> dict:
    """43.5 state + action flags for the Strapi section."""
    settings = get_settings()
    if sync is None:
        state = "not_synced"
    elif sync.sync_status == "in_progress":
        state = "syncing"
    elif sync.sync_status == "draft_created":
        state = "draft_created"
    else:
        state = "failed"
    return {
        "state": state,
        "document_id": sync.strapi_document_id if sync else None,
        "error": sync.error_message if sync else None,
        "can_push": job.status in ("ready", "strapi_draft_created", "failed"),
        "can_update": bool(
            sync and sync.sync_status == "draft_created" and sync.strapi_document_id
        ),
        "admin_url": (
            settings.strapi_admin_panel_url
            if (sync and sync.strapi_document_id)
            else None
        ),
    }
