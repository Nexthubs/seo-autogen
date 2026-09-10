"""Step: content brief (SEO-AUTO-DEV-SPEC.md sections 9, 22).

Input (section 50): keyword (+ dataset metrics when available), the SERP
synthesis, the evidence notes, and the allowed internal link markers.

Checkpoint (section 9): persist ``content_briefs`` with the
Pydantic-validated brief, update the job status, and commit.
"""

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import JobStatus
from app.db.models.job import GenerationJob
from app.db.models.research import (
    EvidenceNoteRow,
    SerpSynthesisRow,
)
from app.db.models.internal_link import InternalLinkRule
from app.pipeline.steps._common import llm_model_name, set_llm_prompt
from app.providers.llm.base import LLMProvider
from app.schemas.research import ContentBrief
from app.services.keyword_service import lookup_metrics
from app.services.prompt_service import PromptSpec, load_prompt

logger = logging.getLogger(__name__)

PROMPT_NAME = "content_brief"


def build_user_prompt(
    keyword: str,
    metrics: dict | None,
    synthesis: dict,
    evidence: list[dict],
    allowed_markers: list[str],
    target_function: str | None,
) -> str:
    """Assemble the brief user prompt (section 50)."""
    parts = [f"Target keyword: {keyword}"]
    if metrics:
        parts.append(f"Dataset metrics: {json.dumps(metrics, ensure_ascii=False)}")
    else:
        parts.append(
            "Dataset metrics: (not available — SERP-only mode, do not invent metrics)"
        )
    parts.append(
        "SERP synthesis:\n" + json.dumps(synthesis, ensure_ascii=False, indent=1)
    )
    parts.append(
        "Evidence notes (the ONLY factual claims the article may use):\n"
        + json.dumps(evidence, ensure_ascii=False, indent=1)
    )
    parts.append(
        "Allowed internal link markers (use ONLY these):\n"
        + (", ".join(allowed_markers) if allowed_markers else "(none)")
    )
    if target_function:
        parts.append(f"Target function to promote: {target_function}")
    else:
        parts.append("Target function to promote: (none)")
    parts.append("\nProduce the content brief now.")
    return "\n\n".join(parts)


async def run_content_brief(
    session: Session,
    job: GenerationJob,
    llm: LLMProvider,
    *,
    prompt: PromptSpec | None = None,
) -> ContentBrief:
    """Generate the content brief for the job and persist it."""
    prompt = prompt or load_prompt(PROMPT_NAME)

    job.status = JobStatus.BRIEF_GENERATING.value
    job.current_step = "brief_generating"
    session.flush()

    synthesis_row = session.scalars(
        select(SerpSynthesisRow).where(SerpSynthesisRow.job_id == job.id)
    ).first()
    synthesis = synthesis_row.synthesis if synthesis_row is not None else {}

    evidence = [
        {
            "claim": n.claim,
            "source_title": n.source_title,
            "source_url": n.source_url,
            "source_type": n.source_type,
            "confidence": n.confidence,
            "usage": n.usage,
            "note": n.note,
        }
        for n in session.scalars(
            select(EvidenceNoteRow).where(EvidenceNoteRow.job_id == job.id)
        ).all()
    ]

    allowed_markers = list(
        session.scalars(
            select(InternalLinkRule.marker).where(InternalLinkRule.active.is_(True))
        ).all()
    )

    metrics = lookup_metrics(session, job.keyword)
    metrics_dict = metrics.model_dump(mode="json") if metrics else None

    set_llm_prompt(llm, prompt)
    brief = await llm.generate_structured(
        system_prompt=prompt.content,
        user_prompt=build_user_prompt(
            job.keyword,
            metrics_dict,
            synthesis,
            evidence,
            allowed_markers,
            job.target_function,
        ),
        response_model=ContentBrief,
    )

    # Only markers that actually exist as active rules may be planned
    # (defense in depth: the prompt already restricts them).
    allowed_set = set(allowed_markers)
    brief = brief.model_copy(
        update={
            "internal_link_markers": [
                m for m in brief.internal_link_markers if m in allowed_set
            ]
        }
    )

    from app.db.models.research import ContentBriefRow

    existing = session.scalars(
        select(ContentBriefRow).where(ContentBriefRow.job_id == job.id)
    ).first()
    if existing is not None:
        session.delete(existing)
        session.flush()

    session.add(
        ContentBriefRow(
            job_id=job.id,
            brief=brief.model_dump(mode="json"),
            model=llm_model_name(llm),
            prompt_version=prompt.version,
            prompt_hash=prompt.prompt_hash,
        )
    )

    job.status = JobStatus.OUTLINE_GENERATING.value
    job.current_step = "outline_generating"
    session.commit()

    logger.info(
        "content_brief_done",
        extra={
            "event": "content_brief_done",
            "job_id": str(job.id),
            "prompt_version": prompt.version,
        },
    )
    return brief
