"""Step: image planning (SEO-AUTO-DEV-SPEC.md sections 30-33).

Runs AFTER the final article revision (status ``image_planning``).

Input (section 50): Title + final article + brand visual guideline +
image count ceiling. NEVER full web texts.

The count ceiling is decided PROGRAMMATICALLY (section 31, Image Count
Service); the LLM may reduce it but never exceed it. The plan is
persisted as ``images`` rows (section 46.15) — the generation step
fills in the file results on the same rows.
"""

import json
import logging

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.enums import ImageRole, JobStatus
from app.core.exceptions import ErrorCode, PipelineError
from app.db.models.images import ImageRow
from app.db.models.job import GenerationJob
from app.pipeline.steps._article_common import (
    article_view_json,
    latest_article_version,
)
from app.pipeline.steps._common import llm_model_name, set_llm_prompt
from app.providers.llm.base import LLMProvider
from app.schemas.images import ImagePlan, ImagePlanItem, ImagePlanOutput
from app.services.image_count_service import image_ceiling
from app.services.prompt_service import PromptSpec, brand_guideline_excerpt, load_prompt

logger = logging.getLogger(__name__)

PROMPT_NAME = "image_planner"


def build_user_prompt(
    article_view: str,
    brand_guideline: str,
    word_count: int,
    ceiling: int,
) -> str:
    """Assemble the planner prompt (section 50)."""
    return (
        "Final article (JSON):\n"
        + article_view
        + "\n\nBrand visual guideline (follow it in every prompt):\n"
        + brand_guideline
        + f"\n\nWord count: {word_count}\n"
        + f"Image count ceiling (HARD maximum, decided by the program "
        + f"from the article length — you may use FEWER, never more): "
        + f"{ceiling}\n"
        + "Return the image plan JSON now."
    )


def normalize_plan(raw: ImagePlanOutput, ceiling: int) -> ImagePlan:
    """Clamp + normalize the LLM plan into a valid ``ImagePlan``.

    - Truncate to the ceiling (section 31: the LLM may reduce, never
      exceed — the program enforces the cap).
    - Reassign inline markers/filenames deterministically in reading
      order: inline-1, inline-2 (section 33 / 34 filenames).
    """
    items = list(raw.images)[:ceiling]
    if not items:
        raise PipelineError(
            ErrorCode.IMAGE_PLAN_INVALID,
            "planner returned no images",
        )
    inline_no = 0
    normalized: list[ImagePlanItem] = []
    for item in items:
        if item.role == ImageRole.HERO.value:
            normalized.append(
                item.model_copy(
                    update={
                        "section_heading": None,
                        "insertion_marker": None,
                        "filename": "hero.webp",
                    }
                )
            )
        else:
            inline_no += 1
            marker = f"inline-{inline_no}"
            normalized.append(
                item.model_copy(
                    update={
                        "role": "inline",
                        "insertion_marker": marker,
                        "filename": f"{marker}.webp",
                    }
                )
            )
    try:
        return ImagePlan(total_count=len(normalized), images=normalized)
    except ValueError as exc:
        raise PipelineError(
            ErrorCode.IMAGE_PLAN_INVALID,
            f"invalid image plan: {exc}",
        ) from exc


def persist_plan(
    session: Session,
    job: GenerationJob,
    plan: ImagePlan,
    *,
    prompt: PromptSpec | None = None,
) -> list[ImageRow]:
    """Replace the job's image plan rows (re-run replaces the plan)."""
    session.execute(delete(ImageRow).where(ImageRow.job_id == job.id))
    session.flush()
    rows: list[ImageRow] = []
    for idx, item in enumerate(plan.images):
        row = ImageRow(
            job_id=job.id,
            role=item.role,
            sort_order=idx,
            purpose=item.purpose,
            section_heading=item.section_heading,
            insertion_marker=item.insertion_marker,
            prompt=item.prompt,
            filename=item.filename,
            alt_text=item.alt_text,
            aspect_ratio=item.aspect_ratio,
            provider="",  # filled by the generation step
            prompt_name=prompt.name if prompt else None,
            prompt_version=prompt.version if prompt else None,
            prompt_hash=prompt.prompt_hash if prompt else None,
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


async def run_image_planner(
    session: Session,
    job: GenerationJob,
    llm: LLMProvider,
    *,
    prompt: PromptSpec | None = None,
    image_count_override: int | None = None,
) -> tuple[ImagePlan, list[ImageRow]]:
    """Plan the images for the final article (sections 30-31).

    ``image_count_override`` (the user's manual Image Mode, P8) acts as a
    hard ceiling on top of the word-count ceiling: a manual value can only
    REDUCE the count the word count already allows, never expand it. The
    1..3 range is enforced upstream (API/form) and defensively here.

    Returns the validated plan and its persisted rows. Leaves the job
    at ``image_generating`` for the generation step.
    """
    prompt = prompt or load_prompt(PROMPT_NAME)

    job.status = JobStatus.IMAGE_PLANNING.value
    job.current_step = "image_planning"
    session.flush()

    version = latest_article_version(session, job)
    if version is None:
        raise PipelineError(
            ErrorCode.IMAGE_PLAN_INVALID,
            "no final article to plan images for — run the article "
            "pipeline first",
        )

    word_count = len(version.body_markdown.split())
    ceiling = image_ceiling(word_count)
    if image_count_override is not None:
        # Manual Image Mode is a hard cap: it may only reduce the word-count
        # ceiling, never expand it (sections 30-31). Defensive clamp 1..3.
        from app.services.image_count_service import (
            IMAGE_MAX_COUNT,
            IMAGE_MIN_COUNT,
        )

        override = max(IMAGE_MIN_COUNT, min(image_count_override, IMAGE_MAX_COUNT))
        ceiling = min(ceiling, override)
    from app.core.config import get_settings

    guideline = brand_guideline_excerpt(get_settings())

    user_prompt = build_user_prompt(
        article_view_json(version), guideline, word_count, ceiling
    )

    set_llm_prompt(llm, prompt)
    raw: ImagePlanOutput = await llm.generate_structured(
        system_prompt=prompt.content,
        user_prompt=user_prompt,
        response_model=ImagePlanOutput,
        temperature=0.3,
    )
    plan = normalize_plan(raw, ceiling)
    rows = persist_plan(session, job, plan, prompt=prompt)

    job.status = JobStatus.IMAGE_GENERATING.value
    job.current_step = "image_generating"
    session.commit()

    logger.info(
        "image_plan_done",
        extra={
            "event": "image_plan_done",
            "job_id": str(job.id),
            "word_count": word_count,
            "ceiling": ceiling,
            "planned_count": plan.total_count,
            "roles": [i.role for i in plan.images],
            "model": llm_model_name(llm),
            "prompt_version": prompt.version,
        },
    )
    return plan, rows
