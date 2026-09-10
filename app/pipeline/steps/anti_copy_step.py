"""Step: anti-copy check (SEO-AUTO-DEV-SPEC.md section 29).

Programmatic (no LLM): the draft's sentences are compared with the 5
competitor sources via RapidFuzz. Flagged overlaps are persisted as an
``anticopy`` review on the article version and fed to the Reviser.

This is NOT a pass/fail duplication rate — the report is a list of
``possible_source_overlap`` flags.
"""

import logging

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import ErrorCode, PipelineError
from app.db.models.article import ArticleVersionRow
from app.db.models.job import GenerationJob
from app.pipeline.steps._article_common import (
    load_competitor_texts,
    persist_review,
)
from app.services.anti_copy import check_anti_copy
from app.schemas.article import AntiCopyReport

logger = logging.getLogger(__name__)

REVIEW_TYPE = "anticopy"


def run_anti_copy_check(
    session: Session,
    job: GenerationJob,
    version: ArticleVersionRow,
) -> AntiCopyReport:
    """Compare one article version against the competitor sources and
    persist the report (section 29).
    """
    texts = load_competitor_texts(session, job)
    if not texts:
        raise PipelineError(
            ErrorCode.SOURCE_EMPTY,
            "anti-copy check needs the competitor sources",
        )

    # P9-A: thresholds come from Settings (spec section 29) instead of
    # module constants, so they are tunable via .env per deployment.
    settings = get_settings()
    raw = check_anti_copy(
        version.body_markdown,
        texts,
        min_words=settings.anti_copy_min_overlap_words,
        min_similarity=settings.anti_copy_min_similarity,
    )
    report = AntiCopyReport.model_validate(raw)

    persist_review(
        session,
        job,
        version,
        review_type=REVIEW_TYPE,
        review=report.model_dump(mode="json"),
    )

    logger.info(
        "anti_copy_check_done",
        extra={
            "event": "anti_copy_check_done",
            "job_id": str(job.id),
            "version": version.version,
            "matches": len(report.matches),
            "serious": report.has_serious_overlap,
        },
    )
    return report
