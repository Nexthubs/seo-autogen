"""Step: source extraction (SEO-AUTO-DEV-SPEC.md sections 9, 12.1,
14.1, 14.2, 15).

Checkpoint (spec section 9): for each Top-5 candidate (organic order),
resolve the page from the source cache (TTL) or the extractor, persist
``source_pages`` + ``job_sources``, update the job status, and commit.

Rules:
- Exa receives only DataForSEO URLs (section 15).
- Fresh cache (within SOURCE_CACHE_TTL_HOURS) -> no extractor call
  (section 14.2).
- Duplicate content across URLs counts once; the next organic result
  backfills the Top-5 slot (section 14.1).
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.enums import JobStatus
from app.core.exceptions import ErrorCode, PipelineError
from app.db.models.job import GenerationJob
from app.db.models.serp import SerpResult, SerpRun
from app.db.models.source import JobSource, SourcePage
from app.providers.extractor.base import ContentExtractor
from app.schemas.sources import ExtractedPage
from app.services.cost_ledger import record_cache_hit, record_provider_cost
from app.services.source_cache import SourceCache, page_from_row
from app.services.url_normalizer import content_hash, normalize_url

logger = logging.getLogger(__name__)

TOP_N = 5
SOURCE_ROLE_COMPETITOR = "competitor"


def _latest_serp_run(session: Session, job: GenerationJob) -> SerpRun | None:
    stmt = (
        select(SerpRun)
        .where(SerpRun.job_id == job.id)
        .order_by(SerpRun.created_at.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def _organic_results(session: Session, run: SerpRun) -> list[SerpResult]:
    stmt = (
        select(SerpResult)
        .where(
            SerpResult.serp_run_id == run.id,
            SerpResult.result_type == "organic",
        )
        .order_by(SerpResult.rank)
    )
    return list(session.scalars(stmt).all())


def _upsert_job_source(
    session: Session,
    job_id: object,
    page: SourcePage,
    serp_rank: int | None,
) -> JobSource:
    stmt = select(JobSource).where(
        JobSource.job_id == job_id,
        JobSource.source_page_id == page.id,
    )
    row = session.scalars(stmt).first()
    if row is None:
        row = JobSource(
            job_id=job_id,
            source_page_id=page.id,
            serp_rank=serp_rank,
            source_role=SOURCE_ROLE_COMPETITOR,
        )
    else:
        row.serp_rank = serp_rank
        row.source_role = SOURCE_ROLE_COMPETITOR
    session.add(row)
    return row


async def run_source_extract(
    session: Session,
    job: GenerationJob,
    extractor: ContentExtractor,
    cache: SourceCache | None = None,
    settings: Settings | None = None,
    max_sources: int = TOP_N,
    force_refresh: bool = False,
) -> list[ExtractedPage]:
    """Extract Top-N competitor sources and checkpoint them to the DB.

    ``force_refresh`` (spec section 14.2 "Force Refresh Sources") bypasses
    the TTL cache read — every URL is re-extracted — while ``cache.upsert``
    still refreshes the ``source_pages`` row, so a force refresh is the only
    path that re-fetches a fresh-but-stale cache entry.
    """
    settings = settings or get_settings()
    cache = cache or SourceCache(settings)

    job.status = JobStatus.SOURCE_EXTRACTING.value
    job.current_step = "source_extracting"
    session.flush()

    run = _latest_serp_run(session, job)
    if run is None:
        raise PipelineError(
            ErrorCode.SOURCE_EMPTY,
            "source_extract requires a completed serp_search step",
        )

    organic = _organic_results(session, run)
    if not organic:
        raise PipelineError(
            ErrorCode.SOURCE_EMPTY,
            "no organic results found for job",
        )

    seen_urls: set[str] = set()
    seen_content: set[str] = set()
    pages: list[ExtractedPage] = []
    # R-H04: keep the last extractor failure so ``SOURCE_EMPTY`` can carry a
    # redacted diagnostic raw instead of dropping the provider detail.
    last_extract_error: PipelineError | None = None

    for result in organic:
        if len(pages) == max_sources:
            break
        if not result.url:
            continue
        url = result.url
        norm = normalize_url(url)
        if norm in seen_urls:
            continue
        seen_urls.add(norm)

        # Cache first (section 14.2): fresh row -> no extractor call.
        # force_refresh bypasses the read (the upsert below still refreshes
        # the row); default keeps the TTL fast path (spec 14.2: "默认不重复抓取").
        row = None if force_refresh else cache.find_fresh(session, url)
        if row is not None:
            page = page_from_row(row)
            # R-M02: a cache hit is a real (zero-cost) event — record it so
            # the ledger shows that no new paid call was made for this URL.
            record_cache_hit(
                session,
                job_id=job.id,
                provider=page.extractor or "extractor",
                step="source_extract",
                detail=norm,
            )
            logger.info(
                "source_cache_hit",
                extra={
                    "event": "source_cache_hit",
                    "job_id": str(job.id),
                    "url": norm,
                },
            )
        else:
            try:
                results = await extractor.extract([url])
            except PipelineError as exc:
                # Per-URL failure: skip and let the next organic result
                # backfill the slot (section 14.1). Retain the last failure
                # so an all-sources-failed SOURCE_EMPTY can still expose the
                # provider's (redacted) raw payload for debugging (R-H04).
                last_extract_error = exc
                logger.warning(
                    "source_extract_failed",
                    extra={
                        "event": "source_extract_failed",
                        "job_id": str(job.id),
                        "url": norm,
                        "error_code": exc.error_code.value,
                    },
                )
                continue
            page = next(
                (p for p in results if p.normalized_url == norm),
                None,
            )
            if page is None:
                logger.warning(
                    "source_extract_failed",
                    extra={
                        "event": "source_extract_failed",
                        "job_id": str(job.id),
                        "url": norm,
                        "error_code": "NO_PAGE",
                    },
                )
                continue
            row = cache.upsert(session, page, url)
            # R-M02: every FRESH extraction is a paid call — append it to the
            # cost ledger, which survives a checkpoint reset (unlike the
            # shared ``source_pages`` row, whose provider_cost is overwritten
            # by the next fresh extraction).
            record_provider_cost(
                session,
                job_id=job.id,
                provider=page.extractor or "extractor",
                step="source_extract",
                amount=page.provider_cost,
                detail=norm,
            )
            # Record the fresh extraction's cost on the cache row (spec
            # section 54). M12: ``source_pages`` is a TTL cache SHARED across
            # jobs, so ``provider_cost`` is the cost of the LAST *fresh*
            # extraction of that page — NOT a per-job ledger. A cache hit
            # above never reaches this line: it performs zero extractor calls
            # and therefore adds ZERO incremental cost to this job (the job
            # consumes the already-payed-for page). A fresh extraction pays
            # once and the shared row is refreshed. A missing/None cost stays
            # None (absent), never coalesced into 0.
            row.provider_cost = page.provider_cost

        # Content dedup (section 14.1): same content at different URLs
        # counts once; the next organic result backfills the slot.
        chash = content_hash(page.content_markdown)
        if chash in seen_content:
            logger.info(
                "source_duplicate_content",
                extra={
                    "event": "source_duplicate_content",
                    "job_id": str(job.id),
                    "url": norm,
                },
            )
            continue
        seen_content.add(chash)

        _upsert_job_source(session, job.id, row, result.rank)
        pages.append(page)

    if not pages:
        raise PipelineError(
            ErrorCode.SOURCE_EMPTY,
            "no competitor sources could be extracted",
            raw=last_extract_error.raw if last_extract_error is not None else None,
        )

    job.status = JobStatus.SERP_ANALYZING.value
    job.current_step = "serp_analyzing"
    session.commit()

    logger.info(
        "source_extract_done",
        extra={
            "event": "source_extract_done",
            "job_id": str(job.id),
            "sources": len(pages),
        },
    )
    return pages
