"""Web UI routes (P8 — SEO-AUTO-DEV-SPEC.md section 43).

Pages, deliberately minimal (43: "V1 只做必要页面"):

* ``/``                  — New Article (43.1)
* ``/jobs``              — Job list (43.2)
* ``/jobs/{id}``         — Job detail + HTMX 2-3s polling (43.3)
* ``/articles/{job_id}`` — Article preview (43.4)
* ``/keywords``          — Keyword dataset + workbook import (spec 20)
* ``/prompts``           — Prompt version dashboard (P9-B2, spec 47/48)
* ``/settings``          — Provider health page (spec 57)

Provider status also lives at ``/api/providers/status`` (44/57) and is
shown on the job detail page; ``/settings`` is the standalone page the
spec names.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models.images import ImageRow
from app.db.models.job import GenerationJob
from app.db.session import check_database, check_redis, get_db
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
from app.workers.article_tasks import build_providers

router = APIRouter(tags=["web"])

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

from markdown_it import MarkdownIt  # noqa: E402

# M03: the preview must render Markdown to HTML, but SAFELY. The
# "default" preset escapes raw HTML input (``<script>`` ->
# ``&lt;script&gt;``) and does not emit raw tags, unlike "commonmark"
# which passes raw HTML straight through. It also renders GFM tables
# and links/images. ``linkify`` is off (the linkify extra is not
# installed) so plain URLs are not auto-linked.
_md = MarkdownIt("default")
_md.options["linkify"] = False


def _render_md(text: str) -> str:
    return _md.render(text or "")


def _local_image_url(job: GenerationJob, row: ImageRow) -> str | None:
    """Serve URL for a generated image, or None when the file is absent.

    Images live at ``{data_dir}/articles/{job_id}/images/{filename}`` and
    are served via ``/static/job-images/{job_id}/{filename}`` (section 34).
    """
    if row.local_path and Path(row.local_path).is_file():
        return f"/static/job-images/{job.id}/{row.filename}"
    return None


def _resolve_preview_body(
    session: Session, job: GenerationJob, version
) -> tuple[str, list, ImageRow | None]:
    """M03: render the article body to (escaped) HTML for the preview.

    Reuses the SAME marker pipeline as export/sync — internal links first
    (section 21), then image markers (section 33) — but is TOLERANT: an
    unresolvable internal-link marker is left as literal text and an
    image whose file is missing is simply omitted, instead of failing the
    page (spec 43.4 is about *showing* the article, not re-validating it;
    the pipeline's hard validation already ran before this). The final
    Markdown is rendered with :func:`_render_md`, which escapes raw HTML
    so a body containing ``<script>`` cannot execute (section 60).

    Returns ``(html, links, hero_row)``.
    """
    from app.services.image_markers import (
        insert_image_markers,
        resolve_image_markers,
    )

    linked, links, _validation = resolve_markers(session, version.body_markdown)

    planned = (
        session.query(ImageRow)
        .filter(ImageRow.job_id == job.id)
        .order_by(ImageRow.sort_order)
        .all()
    )
    hero_row = next((r for r in planned if r.role == "hero"), None)
    inlines = [
        r for r in planned if r.role == "inline" and r.insertion_marker
    ]
    images: dict[str, tuple[str, str]] = {}
    for r in inlines:
        url = _local_image_url(job, r)
        if url:
            images[r.insertion_marker] = (r.alt_text, url)

    marked = insert_image_markers(
        linked, [(r.insertion_marker, r.section_heading) for r in inlines]
    )
    resolved = resolve_image_markers(marked, images)
    return _render_md(resolved), links, hero_row


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

    # M03: render the body to safe HTML (internal links + inline images
    # resolved, raw HTML escaped) and grab the hero row for its alt text.
    rendered, links, hero_row = _resolve_preview_body(session, job, version)
    hero = _local_image_url(job, hero_row) if hero_row else None

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
            "hero_alt": hero_row.alt_text if hero_row else "",
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


# ----------------------------------------------------------------------
# 57  Settings / provider health page
# ----------------------------------------------------------------------
def _provider_statuses(settings) -> list[dict]:
    """Seven status rows (spec 57): LLM, DataForSEO, Exa, Image API,
    Strapi, PostgreSQL, Redis.

    Each row reports exactly one of ``Configured`` / ``Missing`` /
    ``Connected`` / ``Failed`` and NEVER a secret (spec 60 — no key,
    token or URL value is echoed). The check is best-effort and tolerant
    (same pattern as ``app.routes.providers``): unconfigured → Missing,
    configured + reachable → Connected, configured + unreachable →
    Failed. The page itself must never raise.
    """
    providers = build_providers(settings)
    specs = (
        ("LLM", "llm", settings.llm_configured),
        ("DataForSEO", "serp", settings.dataforseo_configured),
        ("Exa", "extractor", settings.exa_configured),
        ("Image API", "image", settings.image_configured),
        ("Strapi", "cms", settings.strapi_configured),
    )

    async def _probe() -> dict:
        reachable: dict = {}
        for name, attr, configured in specs:
            provider = getattr(providers, attr)
            try:
                if configured:
                    reachable[name] = bool(await provider.health_check())
                else:
                    reachable[name] = False
            except Exception:  # noqa: BLE001 - status must never raise
                reachable[name] = False
            finally:
                try:
                    await provider.aclose()
                except Exception:  # noqa: BLE001
                    pass
        return reachable

    reachable = asyncio.run(_probe())

    rows: list[dict] = []
    for name, _attr, configured in specs:
        if not configured:
            status = "Missing"
        elif reachable.get(name):
            status = "Connected"
        else:
            status = "Failed"
        rows.append({"name": name, "status": status})

    rows.append(
        {
            "name": "PostgreSQL",
            "status": "Connected" if check_database() else "Failed",
        }
    )
    rows.append(
        {
            "name": "Redis",
            "status": "Connected" if check_redis(settings.redis_url) else "Failed",
        }
    )
    return rows


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    """Provider + infrastructure health (spec 57)."""
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"request": request, "nav": "settings", "rows": _provider_statuses(settings)},
    )
