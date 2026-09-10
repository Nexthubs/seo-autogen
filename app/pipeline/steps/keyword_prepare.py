"""Step: keyword prepare (SEO-AUTO-DEV-SPEC.md sections 8, 9, 19).

First pipeline step for a queued job:
- Look the keyword up in the imported dataset (section 19).
- ``keyword_metrics_available = True`` → Dataset mode: volume/kd/cpc feed
  the research brief; strategy options may apply.
- ``keyword_metrics_available = False`` → SERP-only mode: the job continues
  as normal but downstream steps must NOT let the LLM guess metrics and the
  UI may show "Keyword metrics unavailable" (section 19.2).
"""

import logging

from sqlalchemy.orm import Session

from app.core.enums import JobStatus
from app.db.models.job import GenerationJob
from app.schemas.keyword import KeywordMetrics
from app.services import keyword_service

logger = logging.getLogger(__name__)


def prepare_keyword(session: Session, job: GenerationJob) -> KeywordMetrics | None:
    """Run the keyword-prepare step and checkpoint the job.

    Returns the resolved metrics (None = SERP-only mode) so the next step
    can embed them in the research brief. The job advances to
    ``serp_searching``; it never fails here — an unknown keyword is a valid
    SERP-only run (section 19.2).
    """
    job.status = JobStatus.KEYWORD_PREPARING.value
    job.current_step = "keyword_preparing"
    session.flush()

    metrics = keyword_service.lookup_metrics(session, job.keyword)
    job.keyword_metrics_available = metrics is not None
    session.commit()

    logger.info(
        "keyword_prepare_done",
        extra={
            "event": "keyword_prepare_done",
            "job_id": str(job.id),
            "keyword_metrics_available": job.keyword_metrics_available,
            "cluster": metrics.cluster_name if metrics else None,
            "volume": metrics.volume if metrics else None,
        },
    )

    job.status = JobStatus.SERP_SEARCHING.value
    job.current_step = "serp_searching"
    session.flush()
    return metrics
