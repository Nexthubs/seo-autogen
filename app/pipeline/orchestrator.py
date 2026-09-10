"""Pipeline orchestrator (P8/P9 — SEO-AUTO-DEV-SPEC.md sections 9, 44, 51, 55).

Chains the 15 checkpoint steps (spec section 9) end to end for one job and
is the ONLY place that turns a step failure into a terminal job state. The
individual steps never set ``FAILED`` (each step advances the state machine
and commits on success); the orchestrator catches a
:class:`~app.core.exceptions.PipelineError` (or any unexpected exception) and
persists ``status=failed`` with the stable ``error_code`` / ``error_message``.

P9 additions on top of the P8 full-run contract:

- **Retry by step** (spec section 44/43.3): ``retry_step`` first deletes the
  checkpoint outputs of that step and every later one
  (:func:`app.pipeline.checkpoints.reset_from_step`) and then runs the chain
  from it.
- **Resume from checkpoint** (spec section 9): ``resume_from_step`` skips
  every step that already has its checkpoint row
  (:func:`app.pipeline.checkpoints.step_done`) and re-runs from the first
  incomplete step — nothing is deleted.
- **Force source refresh** (spec section 14.2): ``force_source_refresh``
  re-extracts even fresh cache entries (step 3 only).
- **In-flight cancellation** (spec section 44/43.3): the DB status is
  re-checked between every step; a job cancelled from the web while a worker
  is running stops at the next step boundary without being marked failed.

Providers are injected so tests can drive the whole chain with scripted fakes
and the RQ task can supply the real, configured providers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.enums import JobStatus
from app.core.exceptions import ErrorCode, PipelineError
from app.db.models.job import GenerationJob
from app.pipeline import checkpoints
from app.pipeline.steps.article_reviser import run_article_reviser
from app.pipeline.steps.article_writer import run_article_writer
from app.pipeline.steps.competitor_analysis import run_competitor_analysis
from app.pipeline.steps.content_brief import run_content_brief
from app.pipeline.steps.evidence_research import run_evidence_research
from app.pipeline.steps.image_generate import run_image_generation
from app.pipeline.steps.image_plan import run_image_planner
from app.pipeline.steps.keyword_prepare import prepare_keyword
from app.pipeline.steps.outline import run_outline
from app.pipeline.steps.reviewers import (
    run_fact_review,
    run_seo_review,
    run_style_review,
)
from app.pipeline.steps.serp_search import run_serp_search
from app.pipeline.steps.serp_synthesis import run_serp_synthesis
from app.pipeline.steps.source_extract import TOP_N, run_source_extract
from app.providers.cms.base import CMSProvider
from app.providers.extractor.base import ContentExtractor
from app.providers.image.base import ImageProvider
from app.providers.llm.base import LLMProvider
from app.providers.serp.base import SERPProvider
from app.services.image_count_service import IMAGE_MAX_COUNT, IMAGE_MIN_COUNT
from app.services.llm_metering import MeteredLLMProvider
from app.services.prompt_service import seo_guideline_excerpt
from app.services.source_cache import SourceCache

logger = logging.getLogger(__name__)


@dataclass
class PipelineProviders:
    """The external providers one pipeline run needs.

    Optional so tests can drive a subset of the chain; the RQ task always
    supplies all five.
    """

    llm: LLMProvider | None = None
    serp: SERPProvider | None = None
    extractor: ContentExtractor | None = None
    image: ImageProvider | None = None
    cms: CMSProvider | None = None


def clamp_image_override(value: int | None) -> int | None:
    """Clamp the user's Image Mode to the absolute 1..3 range (spec 31).

    A manual Image Mode is a hard ceiling, never an expansion: an override
    larger than the word-count suggestion still collapses to the suggestion,
    and anything outside 1..3 is rejected upstream by the API/form.
    """
    if value is None:
        return None
    return max(IMAGE_MIN_COUNT, min(value, IMAGE_MAX_COUNT))


_PROVIDER_ERROR_CODES = {
    "llm": ErrorCode.LLM_UNAVAILABLE,
    "serp": ErrorCode.DATAFORSEO_REQUEST_FAILED,
    "extractor": ErrorCode.EXTRACTOR_FAILED,
    "image": ErrorCode.IMAGE_PROVIDER_FAILED,
    "cms": ErrorCode.STRAPI_DRAFT_CREATE_FAILED,
}


def _provider_error(name: str) -> PipelineError:
    return PipelineError(_PROVIDER_ERROR_CODES[name], f"{name} provider is required")


async def _aclose_all(providers: PipelineProviders) -> None:
    """Best-effort teardown of any providers that support ``aclose``."""
    for attr in ("llm", "serp", "extractor", "image", "cms"):
        provider = getattr(providers, attr)
        if provider is None:
            continue
        aclose = getattr(provider, "aclose", None)
        if aclose is None:
            continue
        try:
            await aclose()
        except Exception:  # noqa: BLE001 - teardown must not mask the real error
            logger.warning(
                "provider_aclose_failed",
                extra={"event": "provider_aclose_failed", "provider": attr},
            )


def _cancelled(session: Session, job: GenerationJob) -> bool:
    """DB-truth check for in-flight cancellation (spec 43.3).

    The web cancel route commits ``status=cancelled`` in its own session; a
    running worker must re-read the committed state at every step boundary
    and stop there.
    """
    session.refresh(job)
    return job.status == JobStatus.CANCELLED.value


async def _run_steps(
    session: Session,
    job: GenerationJob,
    providers: PipelineProviders,
    settings: Settings,
    seo_excerpt: str,
    cache: SourceCache,
    image_override: int | None,
    *,
    start_index: int = 1,
    retry_step: int | None = None,
    force_source_refresh: bool = False,
) -> None:
    """Run the checkpoint chain from ``start_index`` (1-based, spec 9).

    Steps below ``start_index`` are skipped; with ``resume_from`` semantics
    the caller has already computed ``start_index`` from the checkpoint rows.
    Each step self-advances ``job.status``/``current_step`` and commits.
    A cancellation committed by the web route between steps stops the run
    quietly (the job keeps ``cancelled``; the orchestrator records the stop).
    """
    # Cost metering (P9-B1, spec section 54): wrap the LLM so every logical
    # call records one llm_usage row (tokens + duration) in this session.
    # The step commits its own rows; the meter only tracks the current step
    # name, set in the loop below before each runner executes.
    meter = MeteredLLMProvider(providers.llm, session, job.id)
    llm = meter

    steps: list[tuple[str, object]] = [
        (
            "keyword_prepare",
            lambda: prepare_keyword(session, job),
        ),
        (
            "serp_search",
            (lambda: run_serp_search(session, job, providers.serp, settings=settings)),
        ),
        (
            "source_extract",
            (
                lambda: run_source_extract(
                    session,
                    job,
                    providers.extractor,
                    cache=cache,
                    settings=settings,
                    max_sources=TOP_N,
                    force_refresh=force_source_refresh,
                )
            ),
        ),
        (
            "competitor_analysis",
            (lambda: run_competitor_analysis(session, job, llm)),
        ),
        (
            "serp_synthesis",
            (lambda: run_serp_synthesis(session, job, llm)),
        ),
        (
            "evidence_research",
            (lambda: run_evidence_research(session, job, llm)),
        ),
        (
            "content_brief",
            (lambda: run_content_brief(session, job, llm)),
        ),
        (
            "outline",
            (
                lambda: run_outline(session, job, llm, guideline_excerpt=seo_excerpt)
            ),
        ),
        (
            "article_writer",
            (
                lambda: run_article_writer(
                    session, job, llm, guideline_excerpt=seo_excerpt
                )
            ),
        ),
        (
            "seo_review",
            (
                lambda: run_seo_review(
                    session, job, llm, guideline_excerpt=seo_excerpt
                )
            ),
        ),
        (
            "fact_review",
            (
                lambda: run_fact_review(
                    session, job, llm, guideline_excerpt=seo_excerpt
                )
            ),
        ),
        (
            "style_review",
            (
                lambda: run_style_review(
                    session, job, llm, guideline_excerpt=seo_excerpt
                )
            ),
        ),
        (
            "article_reviser",
            (lambda: run_article_reviser(session, job, llm)),
        ),
        (
            "image_plan",
            (
                lambda: run_image_planner(
                    session, job, llm, image_count_override=image_override
                )
            ),
        ),
        (
            "image_generate",
            (
                lambda: run_image_generation(session, job, providers.image, settings=settings)
            ),
        ),
    ]
    assert len(steps) == len(checkpoints.STEP_NAMES)

    for index, (name, runner) in enumerate(steps, start=1):
        # In-flight cancellation is honoured at every step boundary.
        if _cancelled(session, job):
            logger.info(
                "pipeline_cancelled_mid_run",
                extra={
                    "event": "pipeline_cancelled_mid_run",
                    "job_id": str(job.id),
                    "stopped_before_step": name,
                },
            )
            return
        if index < start_index:
            logger.info(
                "pipeline_step_skipped",
                extra={
                    "event": "pipeline_step_skipped",
                    "job_id": str(job.id),
                    "step": name,
                    "reason": "resume_from_checkpoint",
                },
            )
            continue

        # Per-step provider prerequisites (only the steps that run).
        if name == "serp_search" and providers.serp is None:
            raise _provider_error("serp")
        if name == "source_extract" and providers.extractor is None:
            raise _provider_error("extractor")
        if name == "image_generate" and providers.image is None:
            raise _provider_error("image")

        meter.current_step = name
        logger.info(
            "pipeline_step_start",
            extra={
                "event": "pipeline_step_start",
                "job_id": str(job.id),
                "step": name,
                "step_index": index,
            },
        )
        result = runner()
        if result is not None and not hasattr(result, "__await__"):
            raise TypeError(f"step {name!r} returned a non-awaitable coroutine")
        if result is not None:
            await result


async def run_job_pipeline(
    session: Session,
    job: GenerationJob,
    providers: PipelineProviders,
    settings: Settings | None = None,
    *,
    retry_step: int | None = None,
    resume_from_step: int | None = None,
    force_source_refresh: bool = False,
) -> GenerationJob:
    """Run the keyword → article pipeline for ``job``.

    ``retry_step`` (1..15): delete that step's checkpoint outputs and every
    later one, then run the chain from that step (spec 43.3 retry by step).
    ``resume_from_step`` (1..15): run the chain from the first step at or
    after that index which has no checkpoint yet; done steps are skipped,
    nothing is deleted (spec section 9 resume from checkpoint). Both are
    optional and mutually exclusive; a plain call is the P8 full run.

    Returns the (refreshed) job. On any step failure the job is committed as
    ``failed`` with a stable ``error_code`` and the original exception is
    re-raised so the caller (RQ task / test) can log or report it. Providers
    are always closed on exit, success or failure.
    """
    settings = settings or get_settings()
    if providers.llm is None:
        # Nothing was created yet that needs closing.
        raise _provider_error("llm")
    if retry_step is not None and resume_from_step is not None:
        raise ValueError("retry_step and resume_from_step are mutually exclusive")
    if retry_step is not None and not 1 <= retry_step <= len(checkpoints.STEP_NAMES):
        raise ValueError(
            f"retry_step must be 1..{len(checkpoints.STEP_NAMES)}, got {retry_step}"
        )
    if resume_from_step is not None and not 1 <= resume_from_step <= len(
        checkpoints.STEP_NAMES
    ):
        raise ValueError(
            f"resume_from_step must be 1..{len(checkpoints.STEP_NAMES)}, "
            f"got {resume_from_step}"
        )

    seo_excerpt = seo_guideline_excerpt(settings)
    image_override = clamp_image_override(job.image_count_override)

    if retry_step is not None:
        removed = checkpoints.reset_from_step(session, job, retry_step)
        session.commit()
        start_index = retry_step
        logger.info(
            "pipeline_retry_from_step",
            extra={
                "event": "pipeline_retry_from_step",
                "job_id": str(job.id),
                "retry_step": retry_step,
                "steps_reset": removed,
                "force_source_refresh": force_source_refresh,
            },
        )
    elif resume_from_step is not None:
        start_index = resume_from_step
    else:
        start_index = 1

    cache = SourceCache(settings) if start_index <= 3 else None

    job.status = (
        JobStatus.KEYWORD_PREPARING.value
        if start_index == 1
        else job.status or JobStatus.QUEUED.value
    )
    job.current_step = (
        "keyword_preparing" if start_index == 1 else job.current_step
    )
    job.started_at = job.started_at or datetime.now(timezone.utc)
    session.commit()

    try:
        await _run_steps(
            session,
            job,
            providers,
            settings,
            seo_excerpt,
            cache,
            image_override,
            start_index=start_index,
            retry_step=retry_step,
            force_source_refresh=force_source_refresh,
        )
        if _cancelled(session, job):
            # The web route already persisted the cancellation; do not
            # override it with a success transition.
            return job
        session.refresh(job)
        if job.status != JobStatus.READY.value:
            # A resume that found every step already checkpointed (e.g. the
            # run was cancelled after step 15) ends ready without rework.
            job.status = JobStatus.READY.value
            job.current_step = "image_generation"
            job.completed_at = datetime.now(timezone.utc)
            session.commit()
        logger.info(
            "pipeline_done",
            extra={
                "event": "pipeline_done",
                "job_id": str(job.id),
                "status": job.status,
            },
        )
        return job
    except PipelineError as error:
        job.status = JobStatus.FAILED.value
        job.current_step = "failed"
        job.error_code = error.error_code.value
        job.error_message = error.message
        job.completed_at = datetime.now(timezone.utc)
        session.commit()
        logger.error(
            "pipeline_failed",
            extra={
                "event": "pipeline_failed",
                "job_id": str(job.id),
                "error_code": error.error_code.value,
                "error_message": error.message,
            },
        )
        raise
    except Exception as error:  # noqa: BLE001 - terminal catch-all (spec 49)
        job.status = JobStatus.FAILED.value
        job.current_step = "failed"
        job.error_code = "UNEXPECTED"
        job.error_message = str(error)
        job.completed_at = datetime.now(timezone.utc)
        session.commit()
        logger.exception(
            "pipeline_failed",
            extra={
                "event": "pipeline_failed",
                "job_id": str(job.id),
                "error_code": "UNEXPECTED",
            },
        )
        raise
    finally:
        await _aclose_all(providers)
