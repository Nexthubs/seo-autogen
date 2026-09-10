"""Step: competitor analysis (SEO-AUTO-DEV-SPEC.md sections 9, 16, 50).

Each competitor source is analyzed separately: the FULL text of ONE
competitor article plus the keyword goes to the CompetitorAnalyzer;
nothing else of that article is given to any other step (section 16 /
50: the five full texts must never reach the ArticleWriter).

Checkpoint (section 9): persist ``competitor_analyses`` with the
Pydantic-validated analysis, update the job status, and commit.
"""

import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.enums import JobStatus
from app.db.models.job import GenerationJob
from app.db.models.research import CompetitorAnalysisRow
from app.db.models.source import JobSource, SourcePage
from app.providers.llm.base import LLMProvider
from app.schemas.research import CompetitorAnalysis
from app.pipeline.steps._common import llm_model_name
from app.services.prompt_service import PromptSpec, load_prompt

logger = logging.getLogger(__name__)

PROMPT_NAME = "competitor_analyzer"

#: Context budget for a single competitor article (spec section 50:
#: "Single competitor page"; section 15 caps the stored text at
#: SOURCE_MAX_CHARS, we cap the LLM context below that).
MAX_COMPETITOR_CHARS = 24000


def build_user_prompt(
    keyword: str, source_id: str, content_markdown: str, title: str | None
) -> str:
    """Assemble the per-competitor user prompt (section 50)."""
    return (
        f"Target keyword: {keyword}\n\n"
        f"Source id: {source_id}\n"
        f"Source title: {title or '(unknown)'}\n\n"
        "Competitor article (full text):\n"
        f"<<<ARTICLE\n{content_markdown}\nARTICLE>>>\n\n"
        "Analyze this single competitor article now."
    )


async def run_competitor_analysis(
    session: Session,
    job: GenerationJob,
    llm: LLMProvider,
    *,
    prompt: PromptSpec | None = None,
) -> list[CompetitorAnalysis]:
    """Analyze every competitor source of the job and checkpoint the
    validated results to ``competitor_analyses``.
    """
    prompt = prompt or load_prompt(PROMPT_NAME)

    job.status = JobStatus.SERP_ANALYZING.value
    job.current_step = "competitor_analyzing"
    session.flush()

    sources = (
        session.scalars(
            select(SourcePage)
            .join(JobSource, JobSource.source_page_id == SourcePage.id)
            .where(JobSource.job_id == job.id)
            .order_by(JobSource.serp_rank)
        )
        .all()
    )
    if not sources:
        from app.core.exceptions import ErrorCode, PipelineError

        raise PipelineError(
            ErrorCode.SOURCE_EMPTY,
            "competitor_analysis requires extracted sources",
        )

    # Per-source idempotency: a step re-run (Retry by step / Resume from
    # checkpoint) must not re-analyse sources that already have a committed
    # row, or the appends below would duplicate them. A *fresh* retry first
    # clears every analysis row via ``reset_from_step``, so its re-analysis
    # sees an empty set and re-does all sources.
    already_analysed = set(
        session.scalars(
            select(CompetitorAnalysisRow.source_page_id).where(
                CompetitorAnalysisRow.job_id == job.id
            )
        ).all()
    )

    analyses: list[CompetitorAnalysis] = []
    for page in sources:
        if page.id in already_analysed:
            continue
        content = page.content_markdown[:MAX_COMPETITOR_CHARS]
        analysis = await llm.generate_structured(
            system_prompt=prompt.content,
            user_prompt=build_user_prompt(
                job.keyword, str(page.id), content, page.title
            ),
            response_model=CompetitorAnalysis,
        )
        # The LLM echoes back the source id; enforce the actual value.
        analysis = analysis.model_copy(update={"source_id": page.id})

        session.add(
            CompetitorAnalysisRow(
                job_id=job.id,
                source_page_id=page.id,
                analysis=analysis.model_dump(mode="json"),
                model=llm_model_name(llm),
                prompt_version=prompt.version,
                prompt_hash=prompt.prompt_hash,
            )
        )
        analyses.append(analysis)

    # The job stays in SERP_ANALYZING: the SERP synthesis step (also part
    # of the analyzing phase) advances the state machine further.
    session.commit()

    logger.info(
        "competitor_analysis_done",
        extra={
            "event": "competitor_analysis_done",
            "job_id": str(job.id),
            "sources": len(analyses),
            "prompt_version": prompt.version,
        },
    )
    return analyses
