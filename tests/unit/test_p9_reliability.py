"""P9-A unit: core reliability (spec sections 9, 14.1/14.2, 43.3, 44, 51).

Drives the checkpoint map, per-step retry / resume / force-refresh logic and
the retry-route modes with a SQLite override and no real Redis / LLM / SERP /
extractor / image / CMS. Enqueueing is monkeypatched so the run *options* a
retry posts are captured and asserted on. The Tavily extractor is driven with
an httpx MockTransport (no network).

Skipped nothing: every test here runs on SQLite / in-memory HTTP, so the whole
file is safe on a machine without the integration database.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models  # noqa: F401 - register all models on Base
from app.db.base import Base
from app.db.models.article import ArticleReviewRow, ArticleVersionRow
from app.db.models.images import ImageRow
from app.db.models.job import GenerationJob
from app.db.models.research import (
    ArticleOutlineRow,
    CompetitorAnalysisRow,
    ContentBriefRow,
    EvidenceNoteRow,
    SerpSynthesisRow,
)
from app.db.models.serp import SerpResult, SerpRun
from app.db.models.source import JobSource, SourcePage
from app.db.session import get_db
from app.core.config import Settings
from app.core.enums import JobStatus
from app.core.exceptions import ErrorCode, PipelineError
from app.main import create_app
from app.pipeline import checkpoints
from app.pipeline.orchestrator import PipelineProviders, run_job_pipeline
from app.pipeline.steps.source_extract import run_source_extract
from app.providers.extractor.base import ContentExtractor
from app.providers.extractor.tavily import (
    FallingBackExtractor,
    TavilyContentExtractor,
    build_extractor,
)
from app.providers.extractor.exa import ExaContentExtractor
from app.schemas.sources import ExtractedPage
from app.services.source_cache import SourceCache, url_hash

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def _settings() -> Settings:
    return Settings(
        llm_base_url="http://llm.test/v1",
        llm_api_key="test-key",
        llm_model="test-model",
        _env_file=None,
    )


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    with TestSession() as session:
        yield session


def _make_job(
    session: "Session",
    status=JobStatus.QUEUED.value,
    started: bool = False,
) -> GenerationJob:
    job = GenerationJob(
        keyword="p9unit retry resume refresh",
        status=status,
        target_function="coach",
        strategy="auto",
    )
    if started:
        job.started_at = NOW  # keyword_prepare's "done" signal
    session.add(job)
    session.commit()
    return job


def _seed_full_chain(db, job, *, image_partial: bool = False) -> None:
    """Seed every one of the 15 step checkpoints so all steps read "done".

    ``image_partial=True`` leaves one image row without a ``local_path`` so
    ``image_generate`` (15) is *not* done while ``image_plan`` (14) is.
    Used by the checkpoint/reset tests: after seeding the full chain, a
    ``reset_from_step(N)`` deletes exactly steps N..15, so ``first_incomplete``
    lands on the boundary N.
    """
    # step 1: keyword_prepare (started_at is the done signal)
    job.started_at = NOW
    job.keyword_metrics_available = True
    # step 2: serp_search (SerpRun requires location/language/device)
    run = SerpRun(
        job_id=job.id,
        provider="dataforseo",
        query=job.keyword,
        location_code=2342,
        language_code="en",
        device="desktop",
        raw_response={},
    )
    db.add(run)
    db.flush()
    db.add(SerpResult(serp_run_id=run.id, result_type="organic", rank=1,
                      title="t", url="https://a.example.com", domain="a.example.com",
                      snippet="s", raw_item={}))
    # step 3: source_extract
    db.add(JobSource(job_id=job.id, source_page_id=uuid.uuid4(), serp_rank=1))
    # step 4: competitor_analysis
    db.add(CompetitorAnalysisRow(job_id=job.id, source_page_id=uuid.uuid4(), analysis={}))
    # step 5: serp_synthesis
    db.add(SerpSynthesisRow(job_id=job.id, synthesis={}))
    # step 6: evidence_research
    db.add(EvidenceNoteRow(job_id=job.id, claim="c", source_title="t",
                           source_url="https://x.example.com",
                           source_type="study", confidence="high", usage="supported"))
    # step 7: content_brief
    db.add(ContentBriefRow(job_id=job.id, brief={}))
    # step 8: outline
    db.add(ArticleOutlineRow(job_id=job.id, outline={}))
    # step 9: article_writer
    writer = ArticleVersionRow(
        job_id=job.id, version=1, stage="writer", title="T",
        body_markdown="b", seo_title="s", meta_description="m", slug="sl",
    )
    db.add(writer)
    db.flush()
    # steps 10-12: reviews on the writer version
    reviews = []
    for rtype in ("seo", "fact", "style"):
        row = ArticleReviewRow(job_id=job.id, article_version_id=writer.id,
                               review_type=rtype, review={})
        db.add(row)
        reviews.append(row)
    db.flush()
    # step 13: article_reviser
    db.add(ArticleVersionRow(
        job_id=job.id, version=2, stage="revision", title="T2",
        body_markdown="b2", seo_title="s2", meta_description="m2", slug="sl2",
        based_on_reviews={
            row.review_type: {"review_id": str(row.id), "attempt": row.attempt}
            for row in reviews
        },
    ))
    # steps 14-15: image plan + generation
    db.add(ImageRow(job_id=job.id, role="hero", sort_order=1, purpose="p",
                    prompt="pr", filename="hero.webp", alt_text="alt",
                    aspect_ratio="16:9", provider="fake", local_path="/tmp/x.webp"))
    if image_partial:
        db.add(ImageRow(job_id=job.id, role="figure", sort_order=2, purpose="p",
                        prompt="pr", filename="fig.webp", alt_text="alt",
                        aspect_ratio="1:1", provider="fake", local_path=None))
    else:
        db.add(ImageRow(job_id=job.id, role="figure", sort_order=2, purpose="p",
                        prompt="pr", filename="fig.webp", alt_text="alt",
                        aspect_ratio="1:1", provider="fake", local_path="/tmp/y.webp"))
    db.commit()
    return writer


# ===========================================================================
# checkpoints: step_index / step_done / first_incomplete_step / reset
# ===========================================================================
class TestCheckpoints:
    def test_step_index_round_trip(self):
        assert checkpoints.step_index("keyword_prepare") == 1
        assert checkpoints.step_index("image_generate") == 15
        assert len(checkpoints.STEP_NAMES) == 15
        for name in checkpoints.STEP_NAMES:
            assert 1 <= checkpoints.step_index(name) <= 15
        with pytest.raises(ValueError):
            checkpoints.step_index("not_a_step")

    def test_fresh_job_everything_incomplete(self, db):
        job = _make_job(db)
        assert not any(
            checkpoints.step_done(db, job, name) for name in checkpoints.STEP_NAMES
        )
        assert checkpoints.first_incomplete_step(db, job) == 1
        status = checkpoints.checkpoint_status(db, job)
        assert len(status) == 15
        assert status[0] == {
            "name": "keyword_prepare",
            "index": 1,
            "label": "Keyword preparation",
            "done": False,
        }

    def test_seed_full_chain_then_done(self, db):
        job = _make_job(db)
        _seed_full_chain(db, job)

        assert all(
            checkpoints.step_done(db, job, name) for name in checkpoints.STEP_NAMES
        )
        assert checkpoints.first_incomplete_step(db, job) is None

    def test_image_generate_partial_not_done(self, db):
        job = _make_job(db)
        _seed_full_chain(db, job, image_partial=True)
        # plan is done (rows exist) but generation is NOT (one has no path)
        assert checkpoints.step_done(db, job, "image_plan")
        assert not checkpoints.step_done(db, job, "image_generate")
        assert checkpoints.first_incomplete_step(db, job) == 15

    def test_reset_writer_keeps_version_history_deletes_images(self, db):
        job = _make_job(db)
        _seed_full_chain(db, job)
        assert checkpoints.step_done(db, job, "article_writer")
        assert checkpoints.step_done(db, job, "article_reviser")

        # H11: a writer retry (step 9) does NOT delete the immutable article
        # history (writer v1 + revision v2 + the 3 reviews are kept) — it only
        # drops the re-runnable per-run artifacts: the image rows.
        removed = checkpoints.reset_from_step(db, job, 9)
        db.commit()
        assert removed == 15 - 9 + 1
        versions = [
            v.version
            for v in db.scalars(
                select(ArticleVersionRow)
                .where(ArticleVersionRow.job_id == job.id)
                .order_by(ArticleVersionRow.version)
            ).all()
        ]
        assert versions == [1, 2]
        assert db.scalar(
            select(ArticleReviewRow).where(
                ArticleReviewRow.job_id == job.id,
                ArticleReviewRow.review_type == "seo",
            )
        ) is not None
        assert db.scalar(select(ImageRow).where(ImageRow.job_id == job.id)) is None
        # earlier (pre-article) steps preserved
        assert checkpoints.step_done(db, job, "keyword_prepare")
        assert checkpoints.step_done(db, job, "serp_search")
        assert checkpoints.step_done(db, job, "source_extract")
        assert checkpoints.step_done(db, job, "competitor_analysis")
        # Rows remain available as history, but the retried dependency chain
        # is explicitly stale and must restart at the writer.
        assert not checkpoints.step_done(db, job, "article_writer")
        assert not checkpoints.step_done(db, job, "article_reviser")
        assert checkpoints.first_incomplete_step(db, job) == 9

    def test_reset_review_keeps_version_history(self, db):
        job = _make_job(db)
        writer = _seed_full_chain(db, job)
        writer_id = writer.id
        # H11: a review re-run (step 10) keeps the immutable article history
        # (writer draft + revision + the writer's reviews all survive); only
        # the re-runnable per-run artifacts (image rows) are dropped.
        removed = checkpoints.reset_from_step(db, job, 10)
        db.commit()
        assert removed == 15 - 10 + 1
        assert checkpoints.step_done(db, job, "article_writer")
        assert not checkpoints.step_done(db, job, "article_reviser")
        # the writer draft survives with its id intact
        writer_after = db.get(ArticleVersionRow, writer_id)
        assert writer_after is not None
        assert writer_after.stage == "writer"
        # the writer's reviews (attached to the writer version) survive
        assert db.scalar(
            select(ArticleReviewRow).where(
                ArticleReviewRow.article_version_id == writer_after.id,
                ArticleReviewRow.review_type == "seo",
            )
        ) is not None
        # Old rows remain for audit but are invalidated for the current run.
        assert writer_after.invalidated_at is None
        assert checkpoints.first_incomplete_step(db, job) == 10

    def test_fact_retry_invalidates_style_and_revision_for_resume(self, db):
        """R3-H01: retrying Fact Review makes downstream checkpoints stale."""
        from app.pipeline.steps._article_common import persist_review

        job = _make_job(db)
        writer = _seed_full_chain(db, job)

        checkpoints.reset_from_step(db, job, 11)
        db.commit()
        assert checkpoints.step_done(db, job, "seo_review")
        assert not checkpoints.step_done(db, job, "fact_review")
        assert not checkpoints.step_done(db, job, "style_review")
        assert not checkpoints.step_done(db, job, "article_reviser")
        assert checkpoints.first_incomplete_step(db, job) == 11

        # The retried fact review succeeds, then style fails before persisting.
        new_fact = persist_review(
            db,
            job,
            writer,
            review_type="fact",
            review={"issues": [{"verdict": "remove"}]},
        )
        db.commit()
        assert new_fact.attempt == 2
        assert checkpoints.step_done(db, job, "fact_review")
        assert not checkpoints.step_done(db, job, "style_review")
        assert checkpoints.first_incomplete_step(db, job) == 12

    def test_new_writer_draft_invalidates_stale_revision(self, db):
        job = _make_job(db)
        _seed_full_chain(db, job)
        assert checkpoints.step_done(db, job, "article_reviser")

        # H11: simulate the writer re-run appending a NEW draft (v3). The
        # current draft is now v3, so the stale revision (v2, older than the
        # current draft) no longer marks the reviser done, and the fresh
        # draft has no reviews yet -> resume must re-run reviews + reviser.
        db.add(ArticleVersionRow(
            job_id=job.id, version=3, stage="writer", title="T3",
            body_markdown="b3", seo_title="s3", meta_description="m3", slug="sl3"))
        db.commit()
        assert checkpoints.step_done(db, job, "article_writer")
        assert not checkpoints.step_done(db, job, "article_reviser")  # v2 < v3
        assert not checkpoints.step_done(db, job, "seo_review")  # no reviews on v3
        assert checkpoints.first_incomplete_step(db, job) == 10

    def test_reset_invalid_index(self, db):
        job = _make_job(db)
        with pytest.raises(ValueError):
            checkpoints.reset_from_step(db, job, 0)
        with pytest.raises(ValueError):
            checkpoints.reset_from_step(db, job, 16)


# ===========================================================================
# force refresh (spec 14.2): default cache hit vs forced re-extract
# ===========================================================================
class _CountingExtractor(ContentExtractor):
    def __init__(self, content="fresh body text one"):
        self.content = content
        self.calls = 0

    async def extract(self, urls):
        self.calls += 1
        return [
            ExtractedPage(
                url=url,
                normalized_url=url,
                title=f"t {url}",
                content_markdown=self.content,
                extracted_at=datetime.now(timezone.utc),
                extractor="counting",
            )
            for url in urls
        ]

    async def health_check(self):
        return True


def _seed_serp(db, job):
    run = SerpRun(
        job_id=job.id,
        provider="dataforseo",
        query=job.keyword,
        location_code=2342,
        language_code="en",
        device="desktop",
        raw_response={},
    )
    db.add(run)
    db.flush()
    for i, url in enumerate(["https://one.example.com", "https://two.example.com"], start=1):
        db.add(SerpResult(serp_run_id=run.id, result_type="organic", rank=i,
                          title=f"t{i}", url=url, domain=url.split("/")[2],
                          snippet=f"s{i}", raw_item={}))
    db.commit()
    return run


class TestForceRefresh:
    @pytest.mark.asyncio
    async def test_default_is_cache_hit_forced_refetches(self, db, tmp_path):
        settings = _settings()
        settings.source_cache_ttl_hours = 168
        job = _make_job(db)
        _seed_serp(db, job)
        cache = SourceCache(settings)

        ext = _CountingExtractor()
        pages = await run_source_extract(db, job, ext, cache=cache, settings=settings,
                                         max_sources=5, force_refresh=False)
        # two organic urls -> two per-URL extract calls
        assert ext.calls == 2
        # identical content at both urls dedups to one source (section 14.1)
        assert len(pages) == 1

        # A fresh cache row exists for the first url.
        row = cache.find_fresh(db, "https://one.example.com")
        assert row is not None

        ext2 = _CountingExtractor(content="second pass body text")
        await run_source_extract(db, job, ext2, cache=cache, settings=settings,
                                 max_sources=5, force_refresh=False)
        assert ext2.calls == 0  # both urls served from the fresh cache

        ext3 = _CountingExtractor(content="third pass body text")
        await run_source_extract(db, job, ext3, cache=cache, settings=settings,
                                 max_sources=5, force_refresh=True)
        assert ext3.calls == 2  # force refresh re-extracts every url


# ===========================================================================
# M12 / spec section 54: source cache hits are zero-incremental cost
# ===========================================================================
class TestM12CacheHitCost:
    @pytest.mark.asyncio
    async def test_cache_hit_does_not_repay_or_fabricate_cost(self, db):
        settings = _settings()
        settings.source_cache_ttl_hours = 168
        job = _make_job(db)
        _seed_serp(db, job)
        cache = SourceCache(settings)

        class CostingExtractor(_CountingExtractor):
            def __init__(self, cost, **kw):
                super().__init__(**kw)
                self.cost = cost

            async def extract(self, urls):
                pages = await super().extract(urls)
                for p in pages:
                    p.provider_cost = self.cost
                return pages

        # Fresh extraction pays once and stamps the shared cache row.
        ext = CostingExtractor(0.25)
        await run_source_extract(db, job, ext, cache=cache, settings=settings,
                                 max_sources=5, force_refresh=False)
        row = cache.find_fresh(db, "https://one.example.com")
        assert row is not None
        assert float(row.provider_cost) == 0.25

        # A cache hit makes zero extractor calls: it re-pays NOTHING and
        # must NOT overwrite the row with the (unpaid) new cost.
        ext2 = CostingExtractor(0.99)
        await run_source_extract(db, job, ext2, cache=cache, settings=settings,
                                 max_sources=5, force_refresh=False)
        assert ext2.calls == 0
        row = cache.find_fresh(db, "https://one.example.com")
        assert float(row.provider_cost) == 0.25  # last FRESH cost, unchanged

        # A cost the provider never reports stays None (absent) — never a
        # fabricated 0.
        ext3 = CostingExtractor(None)
        await run_source_extract(db, job, ext3, cache=cache, settings=settings,
                                 max_sources=5, force_refresh=True)
        assert ext3.calls == 2
        row = cache.find_fresh(db, "https://one.example.com")
        assert row.provider_cost is None


# ===========================================================================
# orchestrator validation (no DB round-trip needed for the guard clauses)
# ===========================================================================
class TestOrchestratorValidation:
    @pytest.mark.asyncio
    async def test_retry_and_resume_mutually_exclusive(self, db):
        job = _make_job(db)
        providers = PipelineProviders(llm=_NoopLLM())
        with pytest.raises(ValueError):
            await run_job_pipeline(db, job, providers,
                                   retry_step=5, resume_from_step=5)

    @pytest.mark.asyncio
    async def test_retry_step_range(self, db):
        job = _make_job(db)
        providers = PipelineProviders(llm=_NoopLLM())
        for bad in (0, 16, -1):
            with pytest.raises(ValueError):
                await run_job_pipeline(db, job, providers, retry_step=bad)
        for bad in (0, 16):
            with pytest.raises(ValueError):
                await run_job_pipeline(db, job, providers, resume_from_step=bad)

    @pytest.mark.asyncio
    async def test_missing_llm(self, db):
        job = _make_job(db)
        with pytest.raises(PipelineError):
            await run_job_pipeline(db, job, PipelineProviders(llm=None))


class _NoopLLM:
    async def aclose(self):
        return None


# ===========================================================================
# Tavily extractor + fallback (MockTransport, no network)
# ===========================================================================
def _tavily(client, settings=None):
    return TavilyContentExtractor(
        settings=settings or _settings(),
        client=client,
        backoff_seconds=(0.0, 0.0, 0.0),
    )


def _mock_client(handler) -> httpx.AsyncClient:
    """AsyncClient with a MockTransport AND a base_url (the extractor posts
    the relative path ``/extract``)."""
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://tavily.test",
    )


class TestTavilyExtractor:
    def test_success_and_per_url_skip(self):
        def handler(request):
            return httpx.Response(200, json={
                "results": [
                    {"url": "https://ok.example.com", "raw_content": "good content"},
                    {"url": "https://empty.example.com", "raw_content": "   "},
                ],
                "failed_results": [
                    {"url": "https://bad.example.com", "error": "nope"},
                ],
            })

        client = _mock_client(handler)
        ext = _tavily(client)

        import asyncio
        pages = asyncio.run(ext.extract([
            "https://ok.example.com",
            "https://empty.example.com",
            "https://bad.example.com",
        ]))
        urls = {p.normalized_url for p in pages}
        assert "https://ok.example.com" in urls
        assert "https://empty.example.com" not in urls  # blank content skipped
        assert "https://bad.example.com" not in urls  # in failed_results

    def test_empty_results_raises_source_empty(self):
        def handler(request):
            return httpx.Response(200, json={"results": [], "failed_results": []})

        client = _mock_client(handler)
        ext = _tavily(client)
        import asyncio
        with pytest.raises(PipelineError) as exc:
            asyncio.run(ext.extract(["https://x.example.com"]))
        assert exc.value.error_code.value == "SOURCE_EMPTY"

    def test_auth_failure_no_retry(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(401, json={"error": "bad key"})

        client = _mock_client(handler)
        ext = _tavily(client)
        import asyncio
        with pytest.raises(PipelineError) as exc:
            asyncio.run(ext.extract(["https://x.example.com"]))
        assert exc.value.error_code.value == "EXTRACTOR_AUTH_FAILED"
        assert calls["n"] == 1  # 401 is not retried

    def test_retry_then_exhaust(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(503, json={"error": "unavailable"})

        client = _mock_client(handler)
        ext = _tavily(client)
        import asyncio
        with pytest.raises(PipelineError) as exc:
            asyncio.run(ext.extract(["https://x.example.com"]))
        assert exc.value.error_code.value == "EXTRACTOR_FAILED"
        assert calls["n"] == 3  # 429/5xx retried up to 3 attempts

    # ----------------------------------------------------------
    # health_check (audit M15): requires a business success, not just
    # "HTTP < 500" — a bad key (401/403) or a malformed 200 body is
    # never reported as Connected.
    # ----------------------------------------------------------
    def _health_settings(self, **over):
        s = _settings()
        s.tavily_api_key = "tv-key"
        for k, v in over.items():
            setattr(s, k, v)
        return s

    def test_health_unconfigured_false(self):
        client = _mock_client(
            lambda r: httpx.Response(200, json={"results": [], "failed_results": []})
        )
        ext = _tavily(client, settings=_settings())  # no tavily_api_key
        import asyncio
        assert asyncio.run(ext.health_check()) is False

    def test_health_401_false(self):
        client = _mock_client(lambda r: httpx.Response(401, json={"error": "bad key"}))
        ext = _tavily(client, settings=self._health_settings())
        import asyncio
        assert asyncio.run(ext.health_check()) is False

    def test_health_403_false(self):
        client = _mock_client(lambda r: httpx.Response(403, json={"error": "forbidden"}))
        ext = _tavily(client, settings=self._health_settings())
        import asyncio
        assert asyncio.run(ext.health_check()) is False

    def test_health_200_malformed_body_false(self):
        client = _mock_client(lambda r: httpx.Response(200, json={"unexpected": True}))
        ext = _tavily(client, settings=self._health_settings())
        import asyncio
        assert asyncio.run(ext.health_check()) is False

    def test_health_200_results_not_list_false(self):
        client = _mock_client(
            lambda r: httpx.Response(200, json={"results": {}, "failed_results": []})
        )
        ext = _tavily(client, settings=self._health_settings())
        import asyncio
        assert asyncio.run(ext.health_check()) is False

    def test_health_200_shape_true(self):
        client = _mock_client(
            lambda r: httpx.Response(
                200,
                json={"results": [{"url": "u", "raw_content": "c"}], "failed_results": []},
            )
        )
        ext = _tavily(client, settings=self._health_settings())
        import asyncio
        assert asyncio.run(ext.health_check()) is True

    def test_health_transport_error_false(self):
        def handler(request):
            raise httpx.ConnectError("boom")

        client = _mock_client(handler)
        ext = _tavily(client, settings=self._health_settings())
        import asyncio
        assert asyncio.run(ext.health_check()) is False


class TestFallingBackExtractor:
    def test_fallback_fills_missing_urls(self):
        class Primary(ContentExtractor):
            async def extract(self, urls):
                # only serves the first url; the rest are "missing"
                return [ExtractedPage(
                    url=urls[0], normalized_url=urls[0], title="p",
                    content_markdown="primary content",
                    extracted_at=datetime.now(timezone.utc), extractor="primary",
                )]

            async def health_check(self):
                return True

        class Fallback(ContentExtractor):
            def __init__(self):
                self.seen = []

            async def extract(self, urls):
                self.seen = list(urls)
                return [ExtractedPage(
                    url=u, normalized_url=u, title="f", content_markdown="fb",
                    extracted_at=datetime.now(timezone.utc), extractor="fallback",
                ) for u in urls]

            async def health_check(self):
                return True

        fb = Fallback()
        combo = FallingBackExtractor(Primary(), fb)
        import asyncio
        pages = asyncio.run(combo.extract(["https://a.example.com", "https://b.example.com"]))
        # fallback was called with exactly the missing url
        assert fb.seen == ["https://b.example.com"]
        assert {p.normalized_url for p in pages} == {
            "https://a.example.com", "https://b.example.com"}

    def test_both_fail_raises_source_empty(self):
        class Failing(ContentExtractor):
            async def extract(self, urls):
                raise PipelineError(ErrorCode.SOURCE_EMPTY, "none")

            async def health_check(self):
                return False

        combo = FallingBackExtractor(Failing(), Failing())
        import asyncio
        with pytest.raises(PipelineError):
            asyncio.run(combo.extract(["https://a.example.com"]))


class TestBuildExtractor:
    def test_without_key_is_plain_exa(self):
        settings = _settings()
        settings.tavily_api_key = ""
        assert isinstance(build_extractor(settings), ExaContentExtractor)

    def test_with_key_is_falling_back(self):
        settings = _settings()
        settings.tavily_api_key = "tvly-test"
        result = build_extractor(settings)
        assert isinstance(result, FallingBackExtractor)


# ===========================================================================
# retry-route modes (SQLite + monkeypatched enqueue)
# =========================================================================-----------
def _new_job_payload(keyword="p9unit retry route"):
    return {"keyword": keyword, "language": "en", "market": "US",
            "strategy": "auto", "image_count_override": None}


@pytest.fixture()
def route_client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    captured: list[tuple[str, dict | None]] = []

    def fake_enqueue_job(job_id, options=None):
        captured.append((str(job_id), options))

    monkeypatch.setattr("app.workers.article_tasks.enqueue_job", fake_enqueue_job)
    monkeypatch.setattr("app.routes.providers.build_providers",
                        lambda settings=None: PipelineProviders())
    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as client:
        client.captured = captured
        client.db_session = TestSession
        yield client


def _set_status(client, job_id, status):
    with client.db_session() as session:
        job = session.get(GenerationJob, uuid.UUID(job_id))
        job.status = status
        if status == "failed":
            job.error_code = "LLM_UNAVAILABLE"
            job.error_message = "no llm"
        session.commit()


class TestRetryRouteModes:
    def test_full_retry_no_fields_is_p8_contract(self, route_client):
        job_id = route_client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
        _set_status(route_client, job_id, "failed")
        resp = route_client.post(f"/api/jobs/{job_id}/retry")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "queued"
        assert body["enqueued"] is True
        assert body["mode"] == "full"
        # no options dict -> single-arg path
        assert route_client.captured[-1] == (job_id, None)

    def test_step_retry_carries_retry_step(self, route_client):
        job_id = route_client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
        _set_status(route_client, job_id, "failed")
        resp = route_client.post(
            f"/api/jobs/{job_id}/retry", data={"mode": "step", "step": "9"}
        )
        assert resp.status_code == 200
        assert resp.json()["mode"] == "step"
        assert route_client.captured[-1][1] == {"retry_step": 9}

    def test_step_retry_rejects_bad_step(self, route_client):
        job_id = route_client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
        _set_status(route_client, job_id, "failed")
        assert route_client.post(
            f"/api/jobs/{job_id}/retry", data={"mode": "step", "step": "99"}
        ).status_code == 400
        assert route_client.post(
            f"/api/jobs/{job_id}/retry", data={"mode": "step", "step": "x"}
        ).status_code == 400

    def test_resume_retry_carries_first_incomplete(self, route_client):
        job_id = route_client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
        _set_status(route_client, job_id, "failed")
        # a fresh failed job has no checkpoints -> resume from step 1
        resp = route_client.post(
            f"/api/jobs/{job_id}/retry", data={"mode": "resume"}
        )
        assert resp.status_code == 200
        assert resp.json()["resume_from_step"] == 1
        assert route_client.captured[-1][1] == {"resume_from_step": 1}

    def test_force_refresh_adds_flag(self, route_client):
        job_id = route_client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
        _set_status(route_client, job_id, "cancelled")
        resp = route_client.post(
            f"/api/jobs/{job_id}/retry",
            data={"mode": "full", "force_source_refresh": "true"},
        )
        assert resp.status_code == 200
        assert resp.json()["force_source_refresh"] is True
        assert route_client.captured[-1][1] == {"force_source_refresh": True}

    def test_unknown_mode_400(self, route_client):
        job_id = route_client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
        _set_status(route_client, job_id, "failed")
        assert route_client.post(
            f"/api/jobs/{job_id}/retry", data={"mode": "bogus"}
        ).status_code == 400

    def test_non_terminal_still_409(self, route_client):
        job_id = route_client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
        assert route_client.post(
            f"/api/jobs/{job_id}/retry", data={"mode": "step", "step": "5"}
        ).status_code == 409


class TestErrorRawLifecycle:
    """Audit M11: error_raw is stored on failure, exposed in the error
    payload, and cleared on retry."""

    def test_error_raw_exposed_in_get_job(self, route_client):
        job_id = route_client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
        _set_status(route_client, job_id, "failed")
        with route_client.db_session() as session:
            job = session.get(GenerationJob, uuid.UUID(job_id))
            job.error_raw = "Traceback (most recent call last):\n  raw detail"
            session.commit()

        body = route_client.get(f"/api/jobs/{job_id}").json()
        assert body["error"]["error_raw"].endswith("raw detail")

    def test_error_raw_null_when_absent(self, route_client):
        job_id = route_client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
        _set_status(route_client, job_id, "failed")
        body = route_client.get(f"/api/jobs/{job_id}").json()
        assert body["error"]["error_raw"] is None

    def test_retry_clears_error_raw(self, route_client):
        job_id = route_client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
        _set_status(route_client, job_id, "failed")
        with route_client.db_session() as session:
            job = session.get(GenerationJob, uuid.UUID(job_id))
            job.error_raw = "stale traceback"
            session.commit()

        route_client.post(f"/api/jobs/{job_id}/retry")

        with route_client.db_session() as session:
            job = session.get(GenerationJob, uuid.UUID(job_id))
            assert job.status == "queued"
            assert job.error_code is None
            assert job.error_message is None
            assert job.error_raw is None
