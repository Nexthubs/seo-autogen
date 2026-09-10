"""P2 integration: serp_runs / serp_results / source_pages / job_sources
(spec sections 46.5-46.8, 12, 14.1, 14.2).

Runs against the local PostgreSQL (docker container). Skipped when the
database is not reachable, so the suite stays green on CI.

Covers:
  - table shapes match spec 46.5-46.8
  - ORM roundtrip
  - SourceCache TTL: fresh -> hit, expired -> re-extract
  - full P2 pipeline: run_serp_search -> run_source_extract,
    second run -> cache hit without extractor call
  - duplicate content across URLs counted once (backfill)
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete as sa_delete, select, text

from app.core.enums import JobStatus
from app.db.models import GenerationJob, SerpRun, SourcePage
from app.db.models.serp import SerpResult
from app.db.models.source import JobSource
from app.db.session import SessionLocal, check_database, engine
from app.pipeline.steps.serp_search import run_serp_search
from app.pipeline.steps.source_extract import run_source_extract
from app.providers.extractor.base import ContentExtractor
from app.providers.serp.base import SERPProvider
from app.schemas.serp import PAAQuestion, OrganicResult, SERPRequest, SERPResponse
from app.schemas.sources import ExtractedPage
from app.services.source_cache import SourceCache, page_from_row
from app.services.url_normalizer import content_hash, url_hash

pytestmark = pytest.mark.skipif(
    not check_database(), reason="PostgreSQL not reachable"
)

KEYWORD = "anxious attachment no contact"

ORGANIC_URLS = [
    f"https://site{i}.example.com/article-{i}" for i in range(1, 9)
]


# ============================================================
# fakes
# ============================================================
class FakeSERPProvider(SERPProvider):
    """Deterministic SERP provider for pipeline tests."""

    def __init__(self) -> None:
        self.calls = 0

    async def search(self, request: SERPRequest) -> SERPResponse:
        self.calls += 1
        return SERPResponse(
            keyword=request.keyword,
            organic_results=[
                OrganicResult(
                    rank=i + 1,
                    title=f"Title {i + 1}",
                    url=url,
                    domain=url.split("/")[2],
                    snippet=f"Snippet {i + 1}",
                )
                for i, url in enumerate(ORGANIC_URLS)
            ],
            paa_questions=[
                PAAQuestion(question="Why no contact works?", source_url=None),
                PAAQuestion(question="How long is no contact?", source_url=None),
            ],
            related_searches=["no contact rules"],
            raw={"status_code": 200, "keyword": request.keyword},
        )

    async def health_check(self) -> bool:
        return True


class FakeExtractor(ContentExtractor):
    """Returns one page per URL; content is derivable from the URL.

    URLs flagged with the same `content` value produce duplicate
    content (spec section 14.1).
    """

    def __init__(self, *, duplicate_of: dict[str, str] | None = None) -> None:
        self.calls: list[str] = []
        self._duplicate_of = duplicate_of or {}

    async def extract(self, urls: list[str]) -> list[ExtractedPage]:
        self.calls.extend(urls)
        pages = []
        for url in urls:
            content = self._duplicate_of.get(url, f"unique content for {url}")
            pages.append(
                ExtractedPage(
                    url=url,
                    normalized_url=url,
                    title=f"Title {url}",
                    content_markdown=content,
                    extracted_at=datetime.now(timezone.utc),
                    extractor="fake",
                )
            )
        return pages

    async def health_check(self) -> bool:
        return True


# ============================================================
# table shapes (spec 46.5-46.8)
# ============================================================
def _columns(table: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :t"
            ),
            {"t": table},
        ).scalars()
        return set(rows)


def test_serp_runs_table_matches_spec_46_5():
    assert _columns("serp_runs") == {
        "id",
        "job_id",
        "provider",
        "query",
        "location_code",
        "language_code",
        "device",
        "raw_response",
        "provider_cost",
        "created_at",
    }


def test_serp_results_table_matches_spec_46_6():
    assert _columns("serp_results") == {
        "id",
        "serp_run_id",
        "result_type",
        "rank",
        "title",
        "url",
        "normalized_url",
        "domain",
        "snippet",
        "raw_item",
    }


def test_source_pages_table_matches_spec_46_7():
    assert _columns("source_pages") == {
        "id",
        "url",
        "normalized_url",
        "url_hash",
        "title",
        "domain",
        "content_markdown",
        "content_hash",
        "extractor",
        "first_seen_at",
        "last_fetched_at",
        "provider_cost",
    }


def test_job_sources_table_matches_spec_46_8():
    assert _columns("job_sources") == {
        "job_id",
        "source_page_id",
        "serp_rank",
        "source_role",
    }
    with engine.connect() as conn:
        pk = conn.execute(
            text(
                "SELECT a.attname FROM pg_index i "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid "
                "AND a.attnum = ANY(i.indkey) "
                "WHERE i.indrelid = 'job_sources'::regclass AND i.indisprimary"
            )
        ).scalars()
        assert set(pk) == {"job_id", "source_page_id"}


# ============================================================
# SourceCache TTL (spec 14.2)
# ============================================================
def test_source_cache_ttl_hit_and_miss():
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        job = GenerationJob(keyword=KEYWORD, status="queued")
        session.add(job)
        session.commit()

        fresh = SourcePage(
            url=ORGANIC_URLS[0],
            normalized_url=ORGANIC_URLS[0],
            url_hash=url_hash(ORGANIC_URLS[0]),
            domain="site1.example.com",
            content_markdown="cached body",
            content_hash=content_hash("cached body"),
            extractor="fake",
            first_seen_at=now,
            last_fetched_at=now - timedelta(hours=1),
        )
        stale = SourcePage(
            url=ORGANIC_URLS[1],
            normalized_url=ORGANIC_URLS[1],
            url_hash=url_hash(ORGANIC_URLS[1]),
            domain="site2.example.com",
            content_markdown="old body",
            content_hash=content_hash("old body"),
            extractor="fake",
            first_seen_at=now - timedelta(days=10),
            last_fetched_at=now - timedelta(days=8),  # > 168h TTL
        )
        session.add_all([fresh, stale])
        session.commit()
        fresh_id, stale_id, job_id = fresh.id, stale.id, job.id

    cache = SourceCache()
    with SessionLocal() as session:
        hit = cache.find_fresh(session, ORGANIC_URLS[0])
        assert hit is not None and hit.id == fresh_id
        assert page_from_row(hit).content_markdown == "cached body"

        miss = cache.find_fresh(session, ORGANIC_URLS[1])
        assert miss is None
        # but the row itself is still found (expired -> re-extract path)
        assert cache.find_any(session, ORGANIC_URLS[1]).id == stale_id
        assert cache.find_fresh(session, "https://unknown.example.com") is None

        # cleanup
        for row in (
            session.get(SourcePage, fresh_id),
            session.get(SourcePage, stale_id),
        ):
            session.delete(row)
        job_row = session.get(GenerationJob, job_id)
        session.delete(job_row)
        session.commit()


# ============================================================
# full P2 pipeline (spec 12, 12.1, 14.1, 14.2)
# ============================================================
async def test_p2_pipeline_serp_then_extract_then_cache_hit():
    provider = FakeSERPProvider()
    extractor = FakeExtractor()
    cache = SourceCache()

    with SessionLocal() as session:
        job = GenerationJob(keyword=KEYWORD, status="queued")
        session.add(job)
        session.commit()
        job_id = job.id

    # ---- step 1: serp_search -------------------------------
    with SessionLocal() as session:
        job = session.get(GenerationJob, job_id)
        response = await run_serp_search(
            session, job, provider, settings=_p2_settings()
        )

    assert provider.calls == 1
    assert len(response.organic_results) == 8
    assert len(response.paa_questions) == 2

    with SessionLocal() as session:
        job = session.get(GenerationJob, job_id)
        assert job.status == JobStatus.SOURCE_EXTRACTING.value
        run = session.scalars(
            select(SerpRun).where(
                SerpRun.job_id == job_id
            )
        ).first()
        assert run is not None
        assert run.provider == "dataforseo"
        assert run.raw_response["status_code"] == 200  # spec 12.2
        n_organic = len(
            session.scalars(
                select(SerpResult).where(
                    SerpResult.serp_run_id == run.id,
                    SerpResult.result_type == "organic",
                )
            ).all()
        )
        n_paa = len(
            session.scalars(
                select(SerpResult).where(
                    SerpResult.serp_run_id == run.id,
                    SerpResult.result_type == "paa",
                )
            ).all()
        )
        assert n_organic == 8
        assert n_paa == 2

    # ---- step 2: source_extract (fresh, no cache) ----------
    with SessionLocal() as session:
        job = session.get(GenerationJob, job_id)
        pages = await run_source_extract(
            session, job, extractor, cache=cache, settings=_p2_settings()
        )

    # Top-5 unique organic URLs (spec 12.1)
    assert [p.url for p in pages] == ORGANIC_URLS[:5]
    assert extractor.calls == ORGANIC_URLS[:5]

    with SessionLocal() as session:
        job = session.get(GenerationJob, job_id)
        assert job.status == JobStatus.SERP_ANALYZING.value
        job_sources = session.scalars(
            select(JobSource).where(
                JobSource.job_id == job_id
            )
        ).all()
        assert len(job_sources) == 5
        assert [js.serp_rank for js in sorted(job_sources, key=lambda x: x.serp_rank)] == [
            1, 2, 3, 4, 5,
        ]
        assert all(js.source_role == "competitor" for js in job_sources)
        # source_pages rows persisted with url_hash (spec 14)
        sp = session.scalars(
            select(SourcePage).where(
                SourcePage.url == ORGANIC_URLS[0]
            )
        ).first()
        assert sp is not None
        assert len(sp.url_hash) == 64
        assert sp.extractor == "fake"

    # ---- step 3: second run -> cache hit, no extractor call -
    extractor2 = FakeExtractor()
    with SessionLocal() as session:
        job = session.get(GenerationJob, job_id)
        pages2 = await run_source_extract(
            session, job, extractor2, cache=cache, settings=_p2_settings()
        )

    assert [p.url for p in pages2] == ORGANIC_URLS[:5]
    assert extractor2.calls == []  # spec 14.2: fresh TTL -> no extractor call

    _cleanup_p2_session(job_id)


async def test_source_extract_dedupes_duplicate_content():
    """Two organic URLs with identical content count once; the next
    organic result backfills the Top-5 slot (spec 14.1)."""
    provider = FakeSERPProvider()
    # rank-2 URL serves the same content as rank-1 URL
    extractor = FakeExtractor(
        duplicate_of={ORGANIC_URLS[1]: f"unique content for {ORGANIC_URLS[0]}"}
    )
    cache = SourceCache()

    with SessionLocal() as session:
        job = GenerationJob(keyword=KEYWORD, status="queued")
        session.add(job)
        session.commit()
        job_id = job.id

    with SessionLocal() as session:
        job = session.get(GenerationJob, job_id)
        await run_serp_search(session, job, provider, settings=_p2_settings())

    with SessionLocal() as session:
        job = session.get(GenerationJob, job_id)
        pages = await run_source_extract(
            session, job, extractor, cache=cache, settings=_p2_settings()
        )

    # rank 2 dropped as duplicate -> backfilled by rank 6
    assert [p.url for p in pages] == [
        ORGANIC_URLS[0],
        ORGANIC_URLS[2],
        ORGANIC_URLS[3],
        ORGANIC_URLS[4],
        ORGANIC_URLS[5],
    ]

    _cleanup_p2_session(job_id)


def _cleanup_p2_session(job_id: uuid.UUID) -> None:
    with SessionLocal() as session:
        sp_ids = session.scalars(
            select(SourcePage.id).where(SourcePage.url.in_(ORGANIC_URLS))
        ).all()
        session.execute(sa_delete(JobSource).where(JobSource.job_id == job_id))
        session.execute(
            sa_delete(SerpResult).where(
                SerpResult.serp_run_id.in_(
                    select(SerpRun.id).where(SerpRun.job_id == job_id)
                )
            )
        )
        session.execute(sa_delete(SerpRun).where(SerpRun.job_id == job_id))
        session.execute(
            sa_delete(SourcePage).where(SourcePage.id.in_(sp_ids))
        )
        session.delete(session.get(GenerationJob, job_id))
        session.commit()


def _p2_settings():
    from app.core.config import Settings

    return Settings(_env_file=None)
