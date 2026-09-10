"""Step: outline generation + validation (SEO-AUTO-DEV-SPEC.md
sections 9, 23).

The outline is a structured object, never a Markdown string (section
23). Every generated outline is checked by the programmatic validator;
on failure an Outline Repair round runs — at most 2 times. After 2
failed repairs the job fails with ARTICLE_VALIDATION_FAILED.

Checkpoint (section 9): persist ``article_outlines`` (valid flag +
repair count), update the job status, and commit.
"""

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import JobStatus
from app.core.exceptions import ErrorCode, PipelineError
from app.db.models.job import GenerationJob
from app.db.models.research import (
    ArticleOutlineRow,
    ContentBriefRow,
    EvidenceNoteRow,
    SerpSynthesisRow,
)
from app.pipeline.steps._common import llm_model_name
from app.providers.llm.base import LLMProvider
from app.schemas.research import ArticleOutline, ContentBrief
from app.services.prompt_service import PromptSpec, load_prompt
from app.services.outline_validator import validate_outline

logger = logging.getLogger(__name__)

PROMPT_NAME = "outline_generator"
REPAIR_PROMPT_NAME = "outline_repair"

#: Spec section 23: Outline Repair, at most 2 times.
MAX_OUTLINE_REPAIRS = 2


def build_user_prompt(
    guideline_excerpt: str,
    brief: ContentBrief,
    synthesis: dict,
    evidence: list[dict],
) -> str:
    """Assemble the outline user prompt (section 50)."""
    return (
        "SEO guideline excerpt:\n"
        f"<<<GUIDELINE\n{guideline_excerpt}\nGUIDELINE>>>\n\n"
        "Content brief:\n"
        f"{json.dumps(brief.model_dump(mode='json'), ensure_ascii=False, indent=1)}\n\n"
        "SERP synthesis:\n"
        f"{json.dumps(synthesis, ensure_ascii=False, indent=1)}\n\n"
        "Evidence notes:\n"
        f"{json.dumps(evidence, ensure_ascii=False, indent=1)}\n\n"
        "Produce the structured article outline now."
    )


def build_repair_prompt(
    brief: ContentBrief,
    outline: ArticleOutline,
    errors: list[str],
) -> str:
    """Assemble the outline repair prompt (section 23)."""
    return (
        "Content brief:\n"
        f"{json.dumps(brief.model_dump(mode='json'), ensure_ascii=False, indent=1)}\n\n"
        "Previously generated outline:\n"
        f"{json.dumps(outline.model_dump(mode='json'), ensure_ascii=False, indent=1)}\n\n"
        "Validation errors found by the program:\n"
        + "\n".join(f"- {e}" for e in errors)
        + "\n\nReturn the corrected outline now."
    )


async def run_outline(
    session: Session,
    job: GenerationJob,
    llm: LLMProvider,
    *,
    guideline_excerpt: str,
    prompt: PromptSpec | None = None,
    repair_prompt: PromptSpec | None = None,
) -> tuple[ArticleOutline, int]:
    """Generate, validate and persist the article outline.

    Returns the validated outline and the number of repair rounds used.
    """
    prompt = prompt or load_prompt(PROMPT_NAME)
    repair_prompt = repair_prompt or load_prompt(REPAIR_PROMPT_NAME)

    job.status = JobStatus.OUTLINE_GENERATING.value
    job.current_step = "outline_generating"
    session.flush()

    brief_row = session.scalars(
        select(ContentBriefRow).where(ContentBriefRow.job_id == job.id)
    ).first()
    if brief_row is None:
        raise PipelineError(
            ErrorCode.ARTICLE_VALIDATION_FAILED,
            "outline requires a persisted content brief",
        )
    brief = ContentBrief.model_validate(brief_row.brief)

    synthesis_row = session.scalars(
        select(SerpSynthesisRow).where(SerpSynthesisRow.job_id == job.id)
    ).first()
    synthesis = synthesis_row.synthesis if synthesis_row is not None else {}

    evidence = [
        {
            "claim": n.claim,
            "source_type": n.source_type,
            "confidence": n.confidence,
            "usage": n.usage,
        }
        for n in session.scalars(
            select(EvidenceNoteRow).where(EvidenceNoteRow.job_id == job.id)
        ).all()
    ]

    outline = await llm.generate_structured(
        system_prompt=prompt.content,
        user_prompt=build_user_prompt(guideline_excerpt, brief, synthesis, evidence),
        response_model=ArticleOutline,
    )

    errors = validate_outline(outline, brief)
    repair_count = 0
    while errors and repair_count < MAX_OUTLINE_REPAIRS:
        repair_count += 1
        logger.info(
            "outline_repair_started",
            extra={
                "event": "outline_repair_started",
                "job_id": str(job.id),
                "repair": repair_count,
                "errors": len(errors),
            },
        )
        outline = await llm.generate_structured(
            system_prompt=repair_prompt.content,
            user_prompt=build_repair_prompt(brief, outline, errors),
            response_model=ArticleOutline,
        )
        errors = validate_outline(outline, brief)

    if errors:
        raise PipelineError(
            ErrorCode.ARTICLE_VALIDATION_FAILED,
            "outline validation failed after "
            f"{MAX_OUTLINE_REPAIRS} repair rounds: " + "; ".join(errors),
        )

    existing = session.scalars(
        select(ArticleOutlineRow).where(ArticleOutlineRow.job_id == job.id)
    ).first()
    if existing is not None:
        session.delete(existing)
        session.flush()

    session.add(
        ArticleOutlineRow(
            job_id=job.id,
            outline=outline.model_dump(mode="json"),
            model=llm_model_name(llm),
            prompt_version=prompt.version,
            prompt_hash=prompt.prompt_hash,
            valid=True,
            repair_count=repair_count,
        )
    )

    job.status = JobStatus.ARTICLE_GENERATING.value
    job.current_step = "article_generating"
    session.commit()

    logger.info(
        "outline_done",
        extra={
            "event": "outline_done",
            "job_id": str(job.id),
            "sections": len(outline.sections),
            "repairs": repair_count,
            "prompt_version": prompt.version,
        },
    )
    return outline, repair_count
