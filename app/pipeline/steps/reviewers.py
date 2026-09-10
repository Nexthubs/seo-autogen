"""Steps: SEO / Fact / Style reviewers
(SEO-AUTO-DEV-SPEC.md sections 9, 26, 50).

All three reviewers only output problems — they never modify the
article (section 26). Each verdict is persisted in
``article_reviews`` (section 46.14) tied to the article version it
reviewed, with the LLM model and prompt version for provenance.

Input (section 50): SEO Guideline + Brief + Article. The Fact Reviewer
additionally receives the job's evidence notes (section 26.2: the only
authorized factual claims).
"""

import json
import logging

from sqlalchemy.orm import Session

from app.core.enums import JobStatus
from app.db.models.job import GenerationJob
from app.pipeline.steps._article_common import (
    article_view_json,
    latest_article_version,
    load_research_context,
    persist_review,
)
from app.pipeline.steps._common import llm_model_name
from app.providers.llm.base import LLMProvider
from app.schemas.article import FactReview, SEOReview, StyleReview
from app.services.prompt_service import PromptSpec, load_prompt

logger = logging.getLogger(__name__)

_SEO_PROMPT = "seo_reviewer"
_FACT_PROMPT = "fact_reviewer"
_STYLE_PROMPT = "style_reviewer"


def _require_version(session: Session, job: GenerationJob):
    version = latest_article_version(session, job)
    if version is None:
        from app.core.exceptions import ErrorCode, PipelineError

        raise PipelineError(
            ErrorCode.ARTICLE_VALIDATION_FAILED,
            "no article version to review — run the writer first",
        )
    return version


def build_seo_review_prompt(guideline: str, brief: dict, version) -> str:
    return (
        "SEO Guideline:\n" + guideline
        + "\n\nContent Brief:\n"
        + json.dumps(brief, ensure_ascii=False, indent=1)
        + "\n\nArticle under review:\n"
        + article_view_json(version)
        + "\n\nReview the article now."
    )


def build_fact_review_prompt(guideline: str, brief: dict, version, evidence) -> str:
    return (
        "SEO Guideline:\n" + guideline
        + "\n\nContent Brief:\n"
        + json.dumps(brief, ensure_ascii=False, indent=1)
        + "\n\nArticle under review:\n"
        + article_view_json(version)
        + "\n\nEvidence notes (the ONLY authorized factual claims):\n"
        + json.dumps(evidence, ensure_ascii=False, indent=1)
        + "\n\nJudge every factual claim against the evidence notes."
    )


def build_style_review_prompt(guideline: str, brief: dict, version) -> str:
    return (
        "SEO Guideline:\n" + guideline
        + "\n\nContent Brief:\n"
        + json.dumps(brief, ensure_ascii=False, indent=1)
        + "\n\nArticle under review:\n"
        + article_view_json(version)
        + "\n\nReview the article's human style now."
    )


async def run_seo_review(
    session: Session,
    job: GenerationJob,
    llm: LLMProvider,
    *,
    guideline_excerpt: str,
    prompt: PromptSpec | None = None,
) -> SEOReview:
    """SEO review of the latest article version (section 26.1)."""
    prompt = prompt or load_prompt(_SEO_PROMPT)

    job.status = JobStatus.SEO_REVIEWING.value
    job.current_step = "seo_reviewing"
    session.flush()

    version = _require_version(session, job)
    ctx = load_research_context(session, job)

    review = await llm.generate_structured(
        system_prompt=prompt.content,
        user_prompt=build_seo_review_prompt(
            guideline_excerpt, ctx["brief"] or {}, version
        ),
        response_model=SEOReview,
        temperature=0.2,
    )

    persist_review(
        session,
        job,
        version,
        review_type="seo",
        review=review.model_dump(mode="json"),
        model=llm_model_name(llm),
        prompt_version=prompt.version,
        prompt_hash=prompt.prompt_hash,
    )

    job.status = JobStatus.FACT_REVIEWING.value
    job.current_step = "fact_reviewing"
    session.commit()

    logger.info(
        "seo_review_done",
        extra={
            "event": "seo_review_done",
            "job_id": str(job.id),
            "version": version.version,
            "total_score": review.total_score,
        },
    )
    return review


async def run_fact_review(
    session: Session,
    job: GenerationJob,
    llm: LLMProvider,
    *,
    guideline_excerpt: str,
    prompt: PromptSpec | None = None,
) -> FactReview:
    """Fact review against the job's evidence notes (section 26.2)."""
    prompt = prompt or load_prompt(_FACT_PROMPT)

    job.status = JobStatus.FACT_REVIEWING.value
    job.current_step = "fact_reviewing"
    session.flush()

    version = _require_version(session, job)
    ctx = load_research_context(session, job)

    review = await llm.generate_structured(
        system_prompt=prompt.content,
        user_prompt=build_fact_review_prompt(
            guideline_excerpt, ctx["brief"] or {}, version, ctx["evidence"]
        ),
        response_model=FactReview,
        temperature=0.2,
    )

    persist_review(
        session,
        job,
        version,
        review_type="fact",
        review=review.model_dump(mode="json"),
        model=llm_model_name(llm),
        prompt_version=prompt.version,
        prompt_hash=prompt.prompt_hash,
    )

    job.status = JobStatus.STYLE_REVIEWING.value
    job.current_step = "style_reviewing"
    session.commit()

    logger.info(
        "fact_review_done",
        extra={
            "event": "fact_review_done",
            "job_id": str(job.id),
            "version": version.version,
            "issues": len(review.issues),
        },
    )
    return review


async def run_style_review(
    session: Session,
    job: GenerationJob,
    llm: LLMProvider,
    *,
    guideline_excerpt: str,
    prompt: PromptSpec | None = None,
) -> StyleReview:
    """Human style review (section 26.3)."""
    prompt = prompt or load_prompt(_STYLE_PROMPT)

    job.status = JobStatus.STYLE_REVIEWING.value
    job.current_step = "style_reviewing"
    session.flush()

    version = _require_version(session, job)
    ctx = load_research_context(session, job)

    review = await llm.generate_structured(
        system_prompt=prompt.content,
        user_prompt=build_style_review_prompt(
            guideline_excerpt, ctx["brief"] or {}, version
        ),
        response_model=StyleReview,
        temperature=0.2,
    )

    persist_review(
        session,
        job,
        version,
        review_type="style",
        review=review.model_dump(mode="json"),
        model=llm_model_name(llm),
        prompt_version=prompt.version,
        prompt_hash=prompt.prompt_hash,
    )

    job.status = JobStatus.ARTICLE_REVISING.value
    job.current_step = "article_revising"
    session.commit()

    logger.info(
        "style_review_done",
        extra={
            "event": "style_review_done",
            "job_id": str(job.id),
            "version": version.version,
            "score": review.score,
        },
    )
    return review
