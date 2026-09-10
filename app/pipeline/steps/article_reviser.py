"""Step: article revision (SEO-AUTO-DEV-SPEC.md sections 27, 28, 29).

Unified input (section 27):

    Original Draft + SEO Review + Fact Review + Style Review

plus the programmatic anti-copy flags (section 29: "进入 Reviser").

The reviser produces ONE new version. It must not overwrite the
original draft (section 27): the draft stays at version N, the
revision lands at version N+1 (section 28 versioning).
"""

import json
import logging

from sqlalchemy.orm import Session

from app.core.enums import JobStatus
from app.db.models.job import GenerationJob
from app.pipeline.steps._article_common import (
    article_view_json,
    latest_article_version,
    latest_review,
    load_research_context,
    persist_article_version,
)
from app.pipeline.steps._common import llm_model_name
from app.pipeline.steps.anti_copy_step import (
    REVIEW_TYPE as ANTICOPY_TYPE,
)
from app.pipeline.steps.anti_copy_step import run_anti_copy_check
from app.pipeline.steps.article_writer import build_article_document
from app.providers.llm.base import LLMProvider
from app.schemas.article import ArticleDraftOutput, AntiCopyReport
from app.services.prompt_service import PromptSpec, load_prompt

logger = logging.getLogger(__name__)

PROMPT_NAME = "article_reviser"


def build_user_prompt(
    draft_view: str,
    seo_review: dict,
    fact_review: dict,
    style_review: dict,
    anticopy: dict,
) -> str:
    """Assemble the reviser user prompt (section 27 + 29)."""
    return (
        "Original draft:\n" + draft_view
        + "\n\nSEO review:\n"
        + json.dumps(seo_review, ensure_ascii=False, indent=1)
        + "\n\nFact review:\n"
        + json.dumps(fact_review, ensure_ascii=False, indent=1)
        + "\n\nStyle review:\n"
        + json.dumps(style_review, ensure_ascii=False, indent=1)
        + "\n\nAnti-copy flags (possible_source_overlap — rewrite the "
        "flagged passages in completely new wording):\n"
        + json.dumps(anticopy, ensure_ascii=False, indent=1)
        + "\n\nProduce the complete revised article now."
    )


async def run_article_reviser(
    session: Session,
    job: GenerationJob,
    llm: LLMProvider,
    *,
    prompt: PromptSpec | None = None,
) -> tuple[ArticleDraftOutput, AntiCopyReport]:
    """Revise the latest article version into a new one (section 27).

    Returns the revised document and the FINAL anti-copy report (run
    again on the revision, section 29 / DoD "Anti-copy 无严重问题").
    """
    prompt = prompt or load_prompt(PROMPT_NAME)

    job.status = JobStatus.ARTICLE_REVISING.value
    job.current_step = "article_revising"
    session.flush()

    draft = latest_article_version(session, job)
    if draft is None:
        from app.core.exceptions import ErrorCode, PipelineError

        raise PipelineError(
            ErrorCode.ARTICLE_VALIDATION_FAILED,
            "no article draft to revise — run the writer first",
        )

    # Reviews for this version (empty when a review step was skipped).
    seo = latest_review(session, job, draft, "seo") or {}
    fact = latest_review(session, job, draft, "fact") or {}
    style = latest_review(session, job, draft, "style") or {}

    # Anti-copy on the draft (section 29) — its flags enter the reviser.
    draft_anticopy = run_anti_copy_check(session, job, draft)

    ctx = load_research_context(session, job)
    brief = ctx["brief"] or {}

    revised: ArticleDraftOutput = await llm.generate_structured(
        system_prompt=prompt.content,
        user_prompt=build_user_prompt(
            article_view_json(draft), seo, fact, style,
            draft_anticopy.model_dump(mode="json"),
        ),
        response_model=ArticleDraftOutput,
        temperature=0.5,
    )

    doc = build_article_document(
        revised,
        outline_title=draft.title,  # the draft's title == outline title
        brief=brief,
        target_function=job.target_function,
    )

    version_row = persist_article_version(
        session,
        job,
        doc,
        stage="revision",
        model=llm_model_name(llm),
        prompt_name=prompt.name,
        prompt_version=prompt.version,
        prompt_hash=prompt.prompt_hash,
    )

    # Final anti-copy check on the revision (section 29 / DoD).
    final_anticopy = run_anti_copy_check(session, job, version_row)

    job.status = JobStatus.IMAGE_PLANNING.value
    job.current_step = "image_planning"
    session.commit()

    logger.info(
        "article_reviser_done",
        extra={
            "event": "article_reviser_done",
            "job_id": str(job.id),
            "draft_version": draft.version,
            "revision_version": version_row.version,
            "anticopy_matches_final": len(final_anticopy.matches),
            "prompt_version": prompt.version,
        },
    )
    return revised, final_anticopy
