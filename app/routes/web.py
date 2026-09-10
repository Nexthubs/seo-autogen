"""Web UI routes (P8 — SEO-AUTO-DEV-SPEC.md section 43).

Five pages, deliberately minimal (43: "V1 只做必要页面"):

* ``/``                  — New Article (43.1)
* ``/jobs``              — Job list (43.2)
* ``/jobs/{id}``         — Job detail + HTMX 2-3s polling (43.3)
* ``/articles/{job_id}`` — Article preview (43.4)
* ``/keywords``          — Keyword dataset + workbook import (spec 20)

Provider status lives at ``/api/providers/status`` (44/57) and is shown on
the job detail page, not as a standalone page.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models.images import ImageRow
from app.db.models.job import GenerationJob
from app.db.session import get_db
from app.pipeline.steps._article_common import latest_article_version
from app.routes.datasets import api_import_keywords, _strategy_query
from app.routes.jobs import (
    api_cancel_job,
    api_retry_job,
    create_job,
    job_detail_payload,
    job_model_label,
    job_progress,
)
from app.routes.strapi import api_strapi_authors, api_strapi_categories
from app.schemas.job import (
    ALL_STRATEGIES,
    DEFAULT_LANGUAGE,
    DEFAULT_MARKET,
    IMAGE_MODE_AUTO,
    STRATEGY_AUTO,
    CreateJobRequest,
)
from app.services.internal_link_service import resolve_markers
from app.services.keyword_service import query_dataset
from app.services.prompt_dashboard import prompt_dashboard_payload

router = APIRouter(tags=["web"])

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

from markdown_it import MarkdownIt  # noqa: E402

_md = MarkdownIt("commonmark")


def _render_md(text: str) -> str:
    return _md.render(text or "")


def _parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="job not found")


def _job_or_404(session: Session, job_id: uuid.UUID) -> GenerationJob:
    job = session.get(GenerationJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


def _job_dir(job_id: uuid.UUID) -> Path:
    settings = get_settings()
    return Path(settings.data_dir) / "articles" / str(job_id)


# ----------------------------------------------------------------------
# 43.1  New Article
# ----------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
def new_article(
    request: Request, session: Session = Depends(get_db)
) -> HTMLResponse:
    settings = get_settings()
    strategies = [
        ("auto", "Auto"),
        ("high_volume", "High Volume"),
        ("low_kd", "Low KD"),
        ("high_cpc", "High CPC"),
        ("long_tail", "Long Tail"),
        ("pillar", "Pillar"),
    ]
    image_modes = [(IMAGE_MODE_AUTO, "Auto (recommended)"), ("1", "1"), ("2", "2"), ("3", "3")]
    return templates.TemplateResponse(
        request,
        "new_job.html",
        {
            "request": request,
            "nav": "new",
            "default_language": settings.article_default_language or DEFAULT_LANGUAGE,
            "default_market": settings.article_default_market or DEFAULT_MARKET,
            "strategies": strategies,
            "image_modes": image_modes,
            "authors": api_strapi_authors()["items"],
            "categories": api_strapi_categories()["items"],
            "strapi_configured": settings.strapi_configured,
        },
    )


@router.post("/generate", response_class=HTMLResponse)
def generate(
    request: Request,
    keyword: str = Form(...),
    language: str = Form(DEFAULT_LANGUAGE),
    market: str = Form(DEFAULT_MARKET),
    target_function: str = Form(""),
    strategy: str = Form(STRATEGY_AUTO),
    author_document_id: str = Form(""),
    category_document_id: str = Form(""),
    image_mode: str = Form(IMAGE_MODE_AUTO),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """43.1 [Generate] handler.

    Validates the manual Image Mode (must be Auto or 1..3), persists the job
    as ``queued`` and enqueues the RQ pipeline run. The job then proceeds
    even if the browser closes (43.1 acceptance).
    """
    override: int | None = None
    if image_mode and image_mode != IMAGE_MODE_AUTO:
        try:
            override = int(image_mode)
        except ValueError:
            raise HTTPException(status_code=400, detail="image mode must be Auto or 1-3")
        if not 1 <= override <= 3:
            raise HTTPException(
                status_code=400, detail="image mode must be Auto or 1-3"
            )

    try:
        payload = CreateJobRequest(
            keyword=keyword,
            language=language or DEFAULT_LANGUAGE,
            market=market or DEFAULT_MARKET,
            target_function=target_function or None,
            strategy=strategy or STRATEGY_AUTO,
            author_document_id=author_document_id or None,
            category_document_id=category_document_id or None,
            image_count_override=override,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    job, _ = create_job(session, payload)
    # 303 redirect to the job page; its HTMX polling then tracks the run.
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


# ----------------------------------------------------------------------
# 43.2  Job list
# ----------------------------------------------------------------------
@router.get("/jobs", response_class=HTMLResponse)
def jobs_list(
    request: Request, session: Session = Depends(get_db)
) -> HTMLResponse:
    jobs = (
        session.query(GenerationJob)
        .order_by(GenerationJob.created_at.desc())
        .limit(200)
        .all()
    )
    rows = [
        {
            "job": j,
            "progress": job_progress(j),
            "model": job_model_label(session, j),
        }
        for j in jobs
    ]
    return templates.TemplateResponse(
        request, "jobs.html", {"request": request, "nav": "jobs", "rows": rows}
    )


# ----------------------------------------------------------------------
# 43.3  Job detail (+ HTMX polling fragment)
# ----------------------------------------------------------------------
@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(
    request: Request, job_id: str, session: Session = Depends(get_db)
) -> HTMLResponse:
    job = _job_or_404(session, _parse_uuid(job_id))
    data = job_detail_payload(session, job)
    data["request"] = request
    data["job_id"] = job_id
    data["strategies"] = ALL_STRATEGIES
    data["job_detail_payload"] = data
    data["nav"] = "jobs"
    return templates.TemplateResponse(request, "job_detail.html", data)


@router.get("/jobs/{job_id}/fragment")
def job_fragment(
    request: Request, job_id: str, session: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX 2-3s polling fragment (43.3)."""
    job = _job_or_404(session, _parse_uuid(job_id))
    data = job_detail_payload(session, job)
    return templates.TemplateResponse(
        request, "job_fragment.html", {"data": data}
    )


@router.post("/jobs/{job_id}/retry")
async def job_retry(
    request: Request, job_id: str, session: Session = Depends(get_db)
):
    """43.3 Actions: Retry (terminal jobs only). P9-A modes: full / step /
    resume + optional Force Refresh Sources."""
    return await api_retry_job(job_id, request, session=session)


@router.post("/jobs/{job_id}/cancel")
def job_cancel(job_id: str, session: Session = Depends(get_db)):
    """43.3 Actions: Cancel (non-terminal jobs only, P8 minimal)."""
    return api_cancel_job(job_id, session=session)


@router.post("/jobs/{job_id}/sync-strapi")
def job_sync_strapi(job_id: str, session: Session = Depends(get_db)):
    """43.5 [Push Draft to Strapi] / [Update Existing Draft]."""
    from app.routes import jobs as jobs_api

    return jobs_api.api_sync_strapi(job_id, session=session)


# ----------------------------------------------------------------------
# 43.4  Article preview
# ----------------------------------------------------------------------
@router.get("/articles/{job_id}", response_class=HTMLResponse)
def article_preview(
    request: Request, job_id: str, session: Session = Depends(get_db)
) -> HTMLResponse:
    settings = get_settings()
    job = _job_or_404(session, _parse_uuid(job_id))
    version = latest_article_version(session, job)
    if version is None:
        return templates.TemplateResponse(
            request,
            "article_preview.html",
            {
                "request": request,
                "nav": "jobs",
                "job": job,
                "article": None,
                "links": [],
                "hero": None,
            },
            status_code=200,
        )

    rendered, links, _validation = resolve_markers(session, version.body_markdown)

    # Hero image: the first planned hero row (section 34).
    hero_row = session.query(ImageRow).filter(
        ImageRow.job_id == job.id, ImageRow.role == "hero"
    ).first()
    hero = None
    if hero_row and hero_row.local_path and Path(hero_row.local_path).is_file():
        hero = f"/static/job-images/{job.id}/{hero_row.filename}"

    return templates.TemplateResponse(
        request,
        "article_preview.html",
        {
            "request": request,
            "nav": "jobs",
            "job": job,
            "article": {
                "title": version.title,
                "seo_title": version.seo_title,
                "meta_description": version.meta_description,
                "slug": version.slug,
                "rendered": rendered,
            },
            "links": links,
            "hero": hero,
        },
    )


# Serve generated job images (data_dir is outside the app tree).
# Kept intentionally simple: only images under {data_dir}/articles/{job_id}/images.
@router.get("/static/job-images/{job_id}/{filename}", response_class=HTMLResponse)
def job_image(job_id: str, filename: str) -> HTMLResponse:
    settings = get_settings()
    safe_job = _parse_uuid(job_id)
    safe_name = Path(filename).name  # strip any path components
    path = Path(settings.data_dir) / "articles" / str(safe_job) / "images" / safe_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="image not found")
    from fastapi.responses import FileResponse

    return FileResponse(path)  # type: ignore[return-value]


# ----------------------------------------------------------------------
# Keyword dataset (spec 20 / 43.5-adjacent)
# ----------------------------------------------------------------------
@router.get("/keywords", response_class=HTMLResponse)
def keywords_page(
    request: Request,
    strategy: str = "auto",
    session: Session = Depends(get_db),
) -> HTMLResponse:
    query = _strategy_query(strategy)
    rows = query_dataset(session, query)
    return templates.TemplateResponse(
        request,
        "keywords.html",
        {
            "request": request,
            "nav": "keywords",
            "strategy": strategy,
            "rows": rows,
            "strategies": [
                ("auto", "Auto (all)"),
                ("high_volume", "High Volume"),
                ("low_kd", "Low KD"),
                ("high_cpc", "High CPC"),
                ("long_tail", "Long Tail"),
                ("pillar", "Pillar"),
            ],
        },
    )


@router.post("/keywords/import")
async def keywords_import(
    request: Request,
    file: UploadFile = File(...),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Accept the 43.5 keyword-workbook upload and render the report.

    Delegates to the same REST handler the API exposes (spec 44), so form
    and API share one import path.
    """
    try:
        report = await api_import_keywords(file=file, session=session)
    except HTTPException as error:
        # Re-raise as an in-page error instead of a 4xx for the form UX.
        return templates.TemplateResponse(
            request,
            "keywords.html",
            {
                "request": request,
                "nav": "keywords",
                "strategy": "auto",
                "rows": [],
                "strategies": [],
                "import_error": str(error.detail),
                "import_report": None,
            },
            status_code=200,
        )
    return templates.TemplateResponse(
        request,
        "keywords.html",
        {
            "request": request,
            "nav": "keywords",
            "strategy": "auto",
            "rows": [],
            "strategies": [],
            "import_report": report,
            "import_error": None,
        },
    )


@router.get("/prompts", response_class=HTMLResponse)
def prompts_page(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    """Prompt version dashboard (P9-B2 — spec 47/48)."""
    payload = prompt_dashboard_payload(session)
    return templates.TemplateResponse(
        request,
        "prompts.html",
        {
            "request": request,
            "nav": "prompts",
            **payload,
        },
    )
