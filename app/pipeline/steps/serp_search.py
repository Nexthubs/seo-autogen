"""Step: SERP search (SEO-AUTO-DEV-SPEC.md sections 9, 12, 12.1, 12.2).

Checkpoint (spec section 9): fetch the SERP, persist ``serp_runs`` +
``serp_results`` with the full raw payload, record the Top-5 unique
competitor URLs, update job status, and commit — before the next step.
"""

import logging
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.enums import JobStatus
from app.core.exceptions import PipelineError
from app.db.models.job import GenerationJob
from app.db.models.serp import SerpResult, SerpRun
from app.providers.serp.base import SERPProvider
from app.schemas.serp import SERPRequest, SERPResponse
from app.services.cost_ledger import record_provider_cost
from app.services.url_normalizer import normalize_url

logger = logging.getLogger(__name__)

#: Spec section 12.1: exactly 5 unique competitor URLs.
TOP_N = 5


def select_top5_unique(
    response: SERPResponse,
    *,
    existing_urls: set[str] | None = None,
) -> list[tuple[int, str]]:
    """Pick Top-5 unique competitor URLs (spec sections 12.1, 14.1).

    Organic results only, sorted by Google Organic Rank. Deduplication
    is by normalized URL (never by LLM). Already-seen URLs are skipped
    so the caller can backfill with the next organic result when a page
    turns out to be duplicate content.
    """
    existing = existing_urls or set()
    seen: set[str] = set(existing)
    picked: list[tuple[int, str]] = []
    for item in response.organic_results:
        if not item.url:
            continue
        norm = normalize_url(item.url)
        if norm in seen:
            continue
        seen.add(norm)
        picked.append((item.rank, norm))
        if len(picked) == TOP_N:
            break
    return picked


def _domain(url: str) -> str | None:
    return urlsplit(url).hostname


async def run_serp_search(
    session: Session,
    job: GenerationJob,
    provider: SERPProvider,
    settings: Settings | None = None,
) -> SERPResponse:
    """Run the SERP step and checkpoint its results to the database."""
    settings = settings or get_settings()

    job.status = JobStatus.SERP_SEARCHING.value
    job.current_step = "serp_searching"
    session.flush()

    request = SERPRequest(
        keyword=job.keyword,
        location_code=settings.dataforseo_location_code,
        language_code=settings.dataforseo_language_code,
        device=settings.dataforseo_device,  # type: ignore[arg-type]
        depth=settings.dataforseo_depth,
    )
    try:
        response: SERPResponse = await provider.search(request)
    except PipelineError as exc:
        # R3-M02: only record failures for which the provider observed a
        # response/cost field. Network/auth failures without billing evidence
        # must not be guessed as charged.
        if exc.provider_cost_reported:
            record_provider_cost(
                session,
                job_id=job.id,
                provider="dataforseo",
                step="serp_search",
                amount=exc.provider_cost,
                detail=f"{job.keyword}; outcome={exc.error_code.value}",
            )
            # The external side effect already happened. Commit its ledger
            # event independently so the orchestrator's failure rollback
            # cannot erase known spend.
            session.commit()
        raise

    # R-M02: the paid SERP call is recorded in the append-only cost ledger
    # BEFORE any checkpoint row can be deleted by a later reset — the ledger
    # (spec section 54) is independent of ``serp_runs``.
    record_provider_cost(
        session,
        job_id=job.id,
        provider="dataforseo",
        step="serp_search",
        amount=response.provider_cost,
        detail=job.keyword,
    )
    # Decouple confirmed provider spend from later parser/checkpoint storage.
    # If any SerpRun/SerpResult write below fails, this event remains durable.
    session.commit()

    # Persist the full raw payload (spec section 12.2) so re-parsing is
    # possible without another paid SERP call.
    run = SerpRun(
        job_id=job.id,
        provider="dataforseo",
        query=job.keyword,
        location_code=settings.dataforseo_location_code,
        language_code=settings.dataforseo_language_code,
        device=settings.dataforseo_device,
        raw_response=response.raw,
        provider_cost=response.provider_cost,
    )
    session.add(run)
    session.flush()

    for item in response.organic_results:
        session.add(
            SerpResult(
                serp_run_id=run.id,
                result_type="organic",
                rank=item.rank,
                title=item.title,
                url=item.url,
                normalized_url=normalize_url(item.url) if item.url else None,
                domain=_domain(item.url) if item.url else None,
                snippet=item.snippet,
                raw_item={},
            )
        )
    for q in response.paa_questions:
        session.add(
            SerpResult(
                serp_run_id=run.id,
                result_type="paa",
                rank=None,
                title=q.question,
                url=q.source_url,
                normalized_url=(
                    normalize_url(q.source_url) if q.source_url else None
                ),
                domain=_domain(q.source_url) if q.source_url else None,
                snippet=None,
                raw_item={"question": q.question},
            )
        )
    for i, rel in enumerate(response.related_searches, start=1):
        session.add(
            SerpResult(
                serp_run_id=run.id,
                result_type="related",
                rank=i,
                title=rel,
                url=None,
                normalized_url=None,
                domain=None,
                snippet=None,
                raw_item={"title": rel},
            )
        )
    if response.featured_snippet is not None:
        feat = response.featured_snippet
        session.add(
            SerpResult(
                serp_run_id=run.id,
                result_type="featured",
                rank=None,
                title=feat.title,
                url=feat.url,
                normalized_url=normalize_url(feat.url) if feat.url else None,
                domain=_domain(feat.url) if feat.url else feat.domain,
                snippet=feat.snippet,
                raw_item=feat.model_dump(),
            )
        )

    # Top-5 unique competitor URLs are part of the checkpoint (12.1).
    select_top5_unique(response)

    job.status = JobStatus.SOURCE_EXTRACTING.value
    job.current_step = "source_extracting"
    session.commit()

    logger.info(
        "serp_search_done",
        extra={
            "event": "serp_search_done",
            "job_id": str(job.id),
            "organic": len(response.organic_results),
            "paa": len(response.paa_questions),
            "related": len(response.related_searches),
            "featured": 1 if response.featured_snippet is not None else 0,
        },
    )
    return response
