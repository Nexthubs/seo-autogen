"""Step: SERP synthesis (SEO-AUTO-DEV-SPEC.md sections 9, 17, 50).

Input (section 17 / 50): the 5 structured competitor analyses, the PAA
questions, the related searches and the keyword context. The full
competitor texts are NOT part of this prompt.

Checkpoint (section 9): persist one row in ``serp_syntheses`` with the
Pydantic-validated synthesis, update the job status, and commit.
"""

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import JobStatus
from app.db.models.job import GenerationJob
from app.db.models.research import CompetitorAnalysisRow, SerpSynthesisRow
from app.db.models.serp import SerpResult, SerpRun
from app.pipeline.steps._common import llm_model_name
from app.providers.llm.base import LLMProvider
from app.schemas.research import SERPSynthesis
from app.services.prompt_service import PromptSpec, load_prompt

logger = logging.getLogger(__name__)

PROMPT_NAME = "serp_synthesis"


def build_user_prompt(
    keyword: str,
    analyses: list[dict],
    paa_questions: list[str],
    related_searches: list[str],
) -> str:
    """Assemble the synthesis user prompt (section 50)."""
    return (
        f"Target keyword: {keyword}\n\n"
        "Structured competitor analyses (one per source):\n"
        f"{json.dumps(analyses, ensure_ascii=False, indent=1)}\n\n"
        f"People also ask:\n{_numbered(paa_questions)}\n"
        f"Related searches:\n{_numbered(related_searches)}\n\n"
        "Produce the SERP synthesis now."
    )


def _numbered(items: list[str]) -> str:
    if not items:
        return "(none)"
    return "\n".join(f"{i}. {item}" for i, item in enumerate(items, start=1))


def _serp_context(session: Session, job: GenerationJob) -> tuple[list[str], list[str]]:
    """PAA questions + related searches of the job's latest SERP run."""
    run = session.scalars(
        select(SerpRun)
        .where(SerpRun.job_id == job.id)
        .order_by(SerpRun.created_at.desc())
        .limit(1)
    ).first()
    if run is None:
        return [], []
    rows = session.scalars(
        select(SerpResult).where(SerpResult.serp_run_id == run.id)
    ).all()
    paa = [r.title or "" for r in rows if r.result_type == "paa" and r.title]
    related = [r.title or "" for r in rows if r.result_type == "related" and r.title]
    return paa, related


async def run_serp_synthesis(
    session: Session,
    job: GenerationJob,
    llm: LLMProvider,
    *,
    prompt: PromptSpec | None = None,
) -> SERPSynthesis:
    """Synthesize the SERP from the persisted competitor analyses."""
    prompt = prompt or load_prompt(PROMPT_NAME)

    job.status = JobStatus.SERP_ANALYZING.value
    job.current_step = "serp_synthesizing"
    session.flush()

    analysis_rows = list(
        session.scalars(
            select(CompetitorAnalysisRow).where(CompetitorAnalysisRow.job_id == job.id)
        ).all()
    )
    if not analysis_rows:
        from app.core.exceptions import ErrorCode, PipelineError

        raise PipelineError(
            ErrorCode.SOURCE_EMPTY,
            "serp_synthesis requires competitor analyses",
        )

    paa, related = _serp_context(session, job)

    synthesis = await llm.generate_structured(
        system_prompt=prompt.content,
        user_prompt=build_user_prompt(
            job.keyword, [r.analysis for r in analysis_rows], paa, related
        ),
        response_model=SERPSynthesis,
    )

    # One row per job: re-runs of the step replace the previous row.
    existing = session.scalars(
        select(SerpSynthesisRow).where(SerpSynthesisRow.job_id == job.id)
    ).first()
    if existing is not None:
        session.delete(existing)
        session.flush()

    session.add(
        SerpSynthesisRow(
            job_id=job.id,
            synthesis=synthesis.model_dump(mode="json"),
            model=llm_model_name(llm),
            prompt_version=prompt.version,
            prompt_hash=prompt.prompt_hash,
        )
    )

    job.status = JobStatus.EVIDENCE_RESEARCHING.value
    job.current_step = "evidence_researching"
    session.commit()

    logger.info(
        "serp_synthesis_done",
        extra={
            "event": "serp_synthesis_done",
            "job_id": str(job.id),
            "analyses": len(analysis_rows),
            "prompt_version": prompt.version,
        },
    )
    return synthesis
