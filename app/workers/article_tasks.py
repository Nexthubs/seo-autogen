"""RQ task definitions (P8 — SEO-AUTO-DEV-SPEC.md sections 55, 58).

Two tasks exist:

* ``process_job`` — runs the full 15-step pipeline (spec section 9) in a
  fresh event loop and session. The orchestrator owns state transitions and
  provider teardown; the task only logs. Enqueued by the web app on
  job creation, so the work outlives the browser (spec section 43.1:
  closing the browser never stops a job).
* ``sync_strapi_draft`` — pushes the finished article to Strapi as a draft
  (P7 step, on-demand per spec section 43.5; the UI never publishes).

Both tasks create their own ``SessionLocal`` — a web request session is
closed when the request returns, so enqueued work must not share it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from rq import Queue
from redis import Redis

from app.core.config import get_settings
from app.core.exceptions import PipelineError
from app.db.models.job import GenerationJob
from app.db.session import SessionLocal
from app.pipeline.orchestrator import PipelineProviders, run_job_pipeline
from app.pipeline.steps.strapi_sync import run_strapi_sync
from app.providers.cms.strapi_cms import StrapiCMSProvider
from app.providers.extractor.tavily import build_extractor
from app.providers.image.openai_image import OpenAIImageProvider
from app.providers.llm.openai_compatible import OpenAICompatibleLLMProvider
from app.providers.serp.dataforseo import DataForSEOSERPProvider

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"ready", "failed", "cancelled"}


def build_providers(settings=None) -> PipelineProviders:
    """The real, configured providers (one per external service).

    The extractor comes from :func:`build_extractor` (P9-A): Exa is the
    primary, and when ``TAVILY_API_KEY`` is set the wrapper transparently
    falls back to Tavily Extract for any URL Exa misses.
    """
    settings = settings or get_settings()
    return PipelineProviders(
        llm=OpenAICompatibleLLMProvider(settings=settings),
        serp=DataForSEOSERPProvider(settings=settings),
        extractor=build_extractor(settings=settings),
        image=OpenAIImageProvider(settings=settings),
        cms=StrapiCMSProvider(settings=settings),
    )


def get_queue(settings=None) -> Queue:
    settings = settings or get_settings()
    return Queue(
        settings.rq_queue_name,
        connection=Redis.from_url(settings.redis_url),
    )


def enqueue_job(
    job_id: str | uuid.UUID, options: dict | None = None
) -> None:
    """Enqueue a pipeline run for ``job_id``.

    RQ 2.x resolves task functions by dotted path (``pkg.mod.fn``); the
    RQ 1.x ``pkg.mod:fn`` colon form is not resolvable and would make the
    worker raise ``Invalid attribute name`` on every job.

    ``options`` (P9-A) carries the run options as a plain dict that rides
    to the worker as a positional second argument:

    - ``{"retry_step": int}`` — delete that step's checkpoint outputs and
      re-run the chain from it (Retry by step, spec 44/43.3).
    - ``{"resume_from_step": int}`` — skip done checkpoints, re-run from
      the first incomplete one (Resume from checkpoint, spec 9).
    - ``{"force_source_refresh": bool}`` — re-extract even fresh cache
      entries (Force Refresh Sources, spec 14.2).

    ``None`` keeps the P8 full-run contract: the options dict rides as the
    2nd positional arg, so ``process_job`` is called exactly as before.
    """
    settings = get_settings()
    queue = get_queue(settings)
    queue.enqueue(
        "app.workers.article_tasks.process_job",
        str(job_id),
        options or {},
        # RQ's default job timeout (180s) is shorter than a full pipeline
        # run, so it must be set explicitly per job (audit H08).
        timeout=settings.rq_job_timeout_seconds,
    )


def enqueue_strapi_sync(job_id: str | uuid.UUID) -> None:
    """Enqueue the on-demand Strapi draft push for ``job_id``.

    Uses the dotted RQ 2.x task path (see :func:`enqueue_job`).
    """
    settings = get_settings()
    queue = get_queue(settings)
    queue.enqueue(
        "app.workers.article_tasks.sync_strapi_draft",
        str(job_id),
        timeout=settings.rq_job_timeout_seconds,
    )


def process_job(job_id: str, options: dict | None = None) -> None:
    """RQ task: run the pipeline for one job (spec section 9).

    The orchestrator already marks the job ``failed`` with a stable error
    code before re-raising, so this handler only observes and logs — it
    never re-raises (no auto-retry; retry semantics are manual, P9-A).

    ``options`` carries the P9-A run options (see :func:`enqueue_job`):
    ``retry_step`` / ``resume_from_step`` / ``force_source_refresh`` are
    forwarded to the orchestrator; an empty dict is the plain full run.
    """
    options = options or {}
    settings = get_settings()
    try:
        job_pk = uuid.UUID(str(job_id))
    except ValueError:
        logger.error(
            "task_invalid_job_id",
            extra={"event": "task_invalid_job_id", "job_id": job_id},
        )
        return

    with SessionLocal() as session:
        job = session.get(GenerationJob, job_pk)
        if job is None:
            logger.error(
                "task_job_missing",
                extra={"event": "task_job_missing", "job_id": job_id},
            )
            return
        if job.status in TERMINAL_STATUSES:
            logger.warning(
                "task_job_already_terminal",
                extra={
                    "event": "task_job_already_terminal",
                    "job_id": job_id,
                    "status": job.status,
                },
            )
            return

        providers = build_providers(settings)
        try:
            asyncio.run(
                run_job_pipeline(
                    session,
                    job,
                    providers,
                    settings=settings,
                    retry_step=options.get("retry_step"),
                    resume_from_step=options.get("resume_from_step"),
                    force_source_refresh=options.get("force_source_refresh", False),
                )
            )
            logger.info(
                "task_pipeline_done",
                extra={
                    "event": "task_pipeline_done",
                    "job_id": job_id,
                    "options": {k: v for k, v in options.items() if v},
                },
            )
        except PipelineError as error:
            logger.error(
                "task_pipeline_failed",
                extra={
                    "event": "task_pipeline_failed",
                    "job_id": job_id,
                    "error_code": error.error_code.value,
                    "error_message": error.message,
                },
            )
        except Exception:  # noqa: BLE001 - the orchestrator marks UNEXPECTED
            logger.exception(
                "task_pipeline_crash",
                extra={"event": "task_pipeline_crash", "job_id": job_id},
            )


def sync_strapi_draft(job_id: str) -> None:
    """RQ task: push the job's article to Strapi as a draft (section 43.5).

    ``run_strapi_sync`` persists ``strapi_syncs.sync_status`` (plus the job
    error fields) before re-raising on failure, so the handler only logs.
    """
    settings = get_settings()
    try:
        job_pk = uuid.UUID(str(job_id))
    except ValueError:
        logger.error(
            "task_invalid_job_id",
            extra={"event": "task_invalid_job_id", "job_id": job_id},
        )
        return

    with SessionLocal() as session:
        job = session.get(GenerationJob, job_pk)
        if job is None:
            logger.error(
                "task_job_missing",
                extra={"event": "task_job_missing", "job_id": job_id},
            )
            return

        provider = StrapiCMSProvider(settings=settings)
        try:
            asyncio.run(
                run_strapi_sync(session, job, provider, settings=settings)
            )
            logger.info(
                "task_strapi_sync_done",
                extra={"event": "task_strapi_sync_done", "job_id": job_id},
            )
        except PipelineError as error:
            logger.error(
                "task_strapi_sync_failed",
                extra={
                    "event": "task_strapi_sync_failed",
                    "job_id": job_id,
                    "error_code": error.error_code.value,
                    "error_message": error.message,
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "task_strapi_sync_crash",
                extra={"event": "task_strapi_sync_crash", "job_id": job_id},
            )
        finally:
            try:
                asyncio.run(provider.aclose())
            except Exception:  # noqa: BLE001 - teardown must not mask results
                logger.warning(
                    "task_strapi_aclose_failed",
                    extra={"event": "task_strapi_aclose_failed"},
                )
