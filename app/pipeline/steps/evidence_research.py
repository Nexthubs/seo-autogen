"""Step: evidence research (SEO-AUTO-DEV-SPEC.md sections 9, 18).

SEO competitor sources and factual evidence are strictly separated
(section 18): the competitor analyses inform the *topic context*, but
the evidence notes must come from real research sources — a competitor
saying "studies show..." is never cited as a study.

The Writer may later only use research findings / numbers that exist in
the persisted evidence notes (section 18).

Checkpoint (section 9): persist ``evidence_notes``, update the job
status, and commit.
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import JobStatus
from app.db.models.job import GenerationJob
from app.db.models.research import EvidenceNoteRow, SerpSynthesisRow
from app.pipeline.steps._common import llm_model_name, set_llm_prompt
from app.providers.extractor.base import ContentExtractor
from app.providers.llm.base import LLMProvider
from app.schemas.research import EvidenceNote
from app.services.cost_ledger import record_provider_cost
from app.services.evidence_verification import verify_evidence_sources
from app.services.prompt_service import PromptSpec, load_prompt
from pydantic import BaseModel

logger = logging.getLogger(__name__)

PROMPT_NAME = "evidence_research"


class EvidenceResearchOutput(BaseModel):
    """Envelope for the evidence research structured output."""

    notes: list[EvidenceNote]


def build_user_prompt(keyword: str, context: dict) -> str:
    """Assemble the evidence user prompt (section 18, 50)."""
    return (
        f"Target keyword: {keyword}\n\n"
        "SERP context (topics only — competitors are NOT research sources):\n"
        f"- dominant intent: {context.get('dominant_intent', 'unknown')}\n"
        f"- common topics: {', '.join(context.get('common_topics', []))}\n"
        f"- common pain points: {', '.join(context.get('common_pain_points', []))}\n"
        f"- common questions: {', '.join(context.get('common_questions', []))}\n\n"
        "Produce the factual evidence notes now. Remember: only real, "
        "verifiable sources; competitor claims are not evidence."
    )


async def run_evidence_research(
    session: Session,
    job: GenerationJob,
    llm: LLMProvider,
    *,
    prompt: PromptSpec | None = None,
    verifier: ContentExtractor | None = None,
) -> list[EvidenceNote]:
    """Generate the factual evidence notes for the job and persist them.

    When ``verifier`` (an independent content-fetch channel) is provided,
    each note's source URL is verified before the note is trusted; an
    unverifiable source is downgraded to ``confidence="low"`` /
    ``usage="avoid"`` (content guideline: never cite research that cannot be
    confirmed to exist). Pass ``None`` (unit tests) to skip verification.
    """
    prompt = prompt or load_prompt(PROMPT_NAME)

    job.status = JobStatus.EVIDENCE_RESEARCHING.value
    job.current_step = "evidence_researching"
    session.flush()

    synthesis_row = session.scalars(
        select(SerpSynthesisRow).where(SerpSynthesisRow.job_id == job.id)
    ).first()
    context = synthesis_row.synthesis if synthesis_row is not None else {}

    set_llm_prompt(llm, prompt)
    output = await llm.generate_structured(
        system_prompt=prompt.content,
        user_prompt=build_user_prompt(job.keyword, context),
        response_model=EvidenceResearchOutput,
    )

    if not output.notes:
        from app.core.exceptions import ErrorCode, PipelineError

        raise PipelineError(
            ErrorCode.LLM_STRUCTURED_OUTPUT_INVALID,
            "evidence research returned no notes",
        )

    # H09/R-H06: an LLM self-report is not proof a source exists. Verify each
    # note's source URL through the independent extractor AND compare the
    # fetched body against the proposed title/claim/numbers; notes the source
    # does not support are downgraded (soften/avoid) so the writer cannot lean
    # on them, and the corroborating/contradicting excerpt is saved.
    #
    # R-M02: that fetch is itself a paid extractor call the per-artifact cost
    # columns never saw — append it to the independent cost ledger.
    def _record_extraction(page) -> None:
        record_provider_cost(
            session,
            job_id=job.id,
            provider=page.extractor or "extractor",
            step="evidence_research",
            amount=page.provider_cost,
            detail=page.normalized_url or page.url,
        )

    notes = await verify_evidence_sources(
        output.notes, verifier, on_extraction=_record_extraction
    )
    downgraded = sum(
        1 for n in notes if (n.verification_status or "supported") != "supported"
    )

    # One set per job: re-runs replace the previous notes.
    for row in session.scalars(
        select(EvidenceNoteRow).where(EvidenceNoteRow.job_id == job.id)
    ).all():
        session.delete(row)

    for note in notes:
        session.add(
            EvidenceNoteRow(
                job_id=job.id,
                claim=note.claim,
                source_title=note.source_title,
                source_url=note.source_url,
                source_type=note.source_type,
                confidence=note.confidence,
                usage=note.usage,
                note=note.note,
                verification_status=note.verification_status,
                supporting_excerpt=note.supporting_excerpt,
            )
        )

    job.status = JobStatus.BRIEF_GENERATING.value
    job.current_step = "brief_generating"
    session.commit()

    logger.info(
        "evidence_research_done",
        extra={
            "event": "evidence_research_done",
            "job_id": str(job.id),
            "notes": len(notes),
            "downgraded_unverifiable_sources": downgraded,
            "prompt_version": prompt.version,
        },
    )
    return notes
