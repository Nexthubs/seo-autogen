"""Audit R-M02: paid-provider costs live in an append-only ledger.

Before this fix only LLM usage was retained historically. A checkpoint reset
deleted ``serp_runs`` / ``images`` (and their ``provider_cost``), and a
regeneration overwrote the previous image cost — so the real spend of a retry,
a bad-image regeneration, an evidence-verification fetch or a cache hit was
unrecoverable. Section 54 asks for exactly that history.

These tests drive the REAL pipeline steps on SQLite and assert the
``provider_cost_events`` ledger:

* SERP cost survives ``reset_from_step`` and accumulates across retries;
* a fresh extraction is ``charged``; a TTL cache hit is a zero-cost
  ``cache_hit`` event (no duplicate charge);
* the independent evidence-verification fetch is recorded;
* an image regeneration appends a new charge while a valid-file reuse records
  a zero-cost ``reused`` event;
* a provider that reports no cost leaves ``amount`` NULL (unknown, never 0).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import JobStatus
from app.core.exceptions import ErrorCode, PipelineError
from app.db.base import Base
from app.db.models.article import ArticleVersionRow
from app.db.models.cost import ProviderCostEventRow
from app.db.models.images import ImageRow
from app.db.models.job import GenerationJob
from app.db.models.serp import SerpResult, SerpRun
from app.pipeline import checkpoints
from app.pipeline.steps.evidence_research import run_evidence_research
from app.pipeline.steps.image_generate import run_image_generation
from app.pipeline.steps.serp_search import run_serp_search
from app.pipeline.steps.source_extract import run_source_extract
from app.providers.extractor.base import ContentExtractor
from app.providers.image.base import ImageProvider
from app.providers.serp.base import SERPProvider
from app.providers.serp.dataforseo import DataForSEOSERPProvider
from app.schemas.images import GeneratedImage, ImageGenerationRequest
from app.schemas.serp import OrganicResult, SERPRequest, SERPResponse
from app.schemas.sources import ExtractedPage
from app.services.cost_ledger import total_cost
from app.services.image_storage import save_image_bytes

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
PNG_1X1_BYTES = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xe0\x12\x91\x03\x00\x00h\x00=T\x08\xa3\xf7\x00\x00\x00\x00IEND\xaeB`\x82'
URLS = [f"https://r2test-site{i}.example.com/a{i}" for i in range(1, 3)]


def _settings(tmp_path) -> Settings:
    return Settings(
        llm_base_url="http://llm.test/v1",
        llm_api_key="test-key",
        llm_model="test-model",
        dataforseo_request_type="live",
        data_dir=str(tmp_path),
        strapi_frontend_renders_main_image=True,
        _env_file=None,
    )


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            GenerationJob(
                keyword="r2 cost ledger",
                status=JobStatus.QUEUED.value,
                target_function="coach",
                strategy="auto",
            )
        )
        session.commit()
        yield session
    engine.dispose()


def _job(session) -> GenerationJob:
    return session.scalars(select(GenerationJob)).one()


def _events(session, job) -> list[ProviderCostEventRow]:
    return list(
        session.scalars(
            select(ProviderCostEventRow)
            .where(ProviderCostEventRow.job_id == job.id)
            .order_by(ProviderCostEventRow.created_at, ProviderCostEventRow.id)
        ).all()
    )


# ---------------------------------------------------------------- SERP
class FakeSERP(SERPProvider):
    def __init__(self, cost=1.5):
        self.calls = 0
        self._cost = cost

    async def search(self, request: SERPRequest) -> SERPResponse:
        self.calls += 1
        return SERPResponse(
            keyword=request.keyword,
            organic_results=[
                OrganicResult(rank=1, title="T", url=URLS[0], domain="x")
            ],
            paa_questions=[],
            related_searches=[],
            raw={"status_code": 20000},
            provider_cost=self._cost,
        )

    async def health_check(self) -> bool:
        return True


async def test_r_m02_serp_cost_survives_reset_and_accumulates(db, tmp_path):
    job = _job(db)
    provider = FakeSERP(cost=1.5)

    await run_serp_search(db, job, provider, settings=_settings(tmp_path))
    events = _events(db, job)
    assert len(events) == 1
    assert events[0].provider == "dataforseo"
    assert events[0].step == "serp_search"
    assert events[0].amount == Decimal("1.500000")

    # A reset from step 2 deletes the serp_runs row (and its provider_cost)...
    checkpoints.reset_from_step(db, job, 2)
    db.commit()
    assert db.scalars(select(SerpRun).where(SerpRun.job_id == job.id)).first() is None
    # ...but the ledger is untouched.
    assert len(_events(db, job)) == 1

    # Retry appends a second charge instead of replacing the first.
    await run_serp_search(db, job, provider, settings=_settings(tmp_path))
    events = _events(db, job)
    assert len(events) == 2
    assert all(e.kind == "charged" for e in events)
    assert total_cost(db, job.id) == Decimal("3.000000")


async def test_r_m02_unknown_serp_cost_stays_null(db, tmp_path):
    job = _job(db)
    await run_serp_search(
        db, job, FakeSERP(cost=None), settings=_settings(tmp_path)
    )
    event = _events(db, job)[0]
    assert event.amount is None  # unknown, never coalesced into 0
    assert total_cost(db, job.id) is None


async def test_r3_m02_empty_serp_keeps_reported_cost(db, tmp_path):
    """HTTP/business success can still yield no organic result; its cost is
    durable even though the SERP checkpoint fails."""
    raw = {
        "status_code": 20000,
        "cost": 0.05,
        "tasks": [
            {
                "status_code": 20000,
                "cost": 0.05,
                "result": [{"type": "organic", "items": []}],
            }
        ],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=raw)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://dfs.test"
    )
    provider = DataForSEOSERPProvider(
        settings=_settings(tmp_path), client=client, backoff_seconds=(0, 0, 0)
    )
    job = _job(db)
    try:
        with pytest.raises(PipelineError) as caught:
            await run_serp_search(db, job, provider, settings=_settings(tmp_path))
        assert caught.value.error_code is ErrorCode.DATAFORSEO_EMPTY_SERP
        db.rollback()  # mirrors the orchestrator failure path
        events = _events(db, job)
        assert len(events) == 1
        assert events[0].kind == "charged"
        assert events[0].amount == Decimal("0.050000")
        assert "DATAFORSEO_EMPTY_SERP" in (events[0].detail or "")
        assert db.scalar(select(SerpRun).where(SerpRun.job_id == job.id)) is None
    finally:
        await client.aclose()


async def test_r3_m02_observed_response_without_cost_is_unknown(db, tmp_path):
    class FailedAfterResponse(SERPProvider):
        async def search(self, request: SERPRequest) -> SERPResponse:
            raise PipelineError(
                ErrorCode.DATAFORSEO_REQUEST_FAILED,
                "business result invalid",
                provider_cost_reported=True,
            )

        async def health_check(self) -> bool:
            return True

    job = _job(db)
    with pytest.raises(PipelineError):
        await run_serp_search(
            db, job, FailedAfterResponse(), settings=_settings(tmp_path)
        )
    db.rollback()
    event = _events(db, job)[0]
    assert event.amount is None
    assert total_cost(db, job.id) is None


async def test_r3_m02_unobserved_failure_does_not_guess_a_charge(db, tmp_path):
    class NetworkFailure(SERPProvider):
        async def search(self, request: SERPRequest) -> SERPResponse:
            raise PipelineError(
                ErrorCode.DATAFORSEO_REQUEST_FAILED, "network failed"
            )

        async def health_check(self) -> bool:
            return True

    job = _job(db)
    with pytest.raises(PipelineError):
        await run_serp_search(db, job, NetworkFailure(), settings=_settings(tmp_path))
    db.rollback()
    assert _events(db, job) == []


async def test_r3_m02_cost_survives_later_checkpoint_storage_failure(
    db, tmp_path, monkeypatch
):
    """A confirmed charge is independent of later checkpoint persistence."""

    def fail_normalize(_url: str) -> str:
        raise RuntimeError("checkpoint storage failed")

    monkeypatch.setitem(run_serp_search.__globals__, "normalize_url", fail_normalize)
    job = _job(db)
    with pytest.raises(RuntimeError, match="checkpoint storage failed"):
        await run_serp_search(
            db, job, FakeSERP(cost=0.25), settings=_settings(tmp_path)
        )

    db.rollback()  # mirrors the orchestrator failure path
    events = _events(db, job)
    assert len(events) == 1
    assert events[0].amount == Decimal("0.250000")
    assert db.scalar(select(SerpRun).where(SerpRun.job_id == job.id)) is None


# ------------------------------------------------------------- extractor
class FakeExtractor(ContentExtractor):
    def __init__(self, cost=0.05):
        self.calls: list[list[str]] = []
        self._cost = cost

    async def extract(self, urls: list[str]) -> list[ExtractedPage]:
        self.calls.append(list(urls))
        return [
            ExtractedPage(
                url=url,
                normalized_url=url,
                title="T",
                content_markdown=f"Body for {url}",
                extracted_at=NOW,
                extractor="exa",
                provider_cost=self._cost,
            )
            for url in urls
        ]

    async def health_check(self) -> bool:
        return True


def _seed_serp_rows(session, job) -> None:
    run = SerpRun(
        job_id=job.id,
        provider="dataforseo",
        query=job.keyword,
        location_code=2840,
        language_code="en",
        device="desktop",
        raw_response={"status_code": 20000},
    )
    session.add(run)
    session.flush()
    for rank, url in enumerate(URLS, start=1):
        session.add(
            SerpResult(
                serp_run_id=run.id,
                result_type="organic",
                rank=rank,
                url=url,
                raw_item={},
            )
        )
    session.commit()


async def test_r_m02_cache_hit_records_zero_cost_not_a_new_charge(db, tmp_path):
    job = _job(db)
    _seed_serp_rows(db, job)
    extractor = FakeExtractor(cost=0.05)

    await run_source_extract(db, job, extractor, settings=_settings(tmp_path))
    first = _events(db, job)
    assert len(first) == len(URLS)
    assert {e.kind for e in first} == {"charged"}
    assert sum(e.amount for e in first) == Decimal("0.100000")

    # Second run: fresh TTL cache -> zero extractor calls, zero-cost events.
    await run_source_extract(db, job, extractor, settings=_settings(tmp_path))
    events = _events(db, job)
    hits = [e for e in events if e.kind == "cache_hit"]
    assert len(hits) == len(URLS)
    assert all(e.amount == Decimal("0.000000") for e in hits)
    # total charged spend did NOT grow
    assert total_cost(db, job.id) == Decimal("0.100000")


# ------------------------------------------------------------- evidence
class FakeLLM:
    def __init__(self, payload: dict):
        self._payload = payload

    async def generate_structured(self, **kwargs):
        from app.pipeline.steps.evidence_research import EvidenceResearchOutput

        return EvidenceResearchOutput.model_validate(self._payload)

    async def aclose(self) -> None:  # pragma: no cover - parity
        return None


async def test_r_m02_evidence_verification_fetch_is_recorded(db, tmp_path):
    job = _job(db)
    verifier = FakeExtractor(cost=0.02)
    note = {
        "claim": "Attachment styles affect adult relationships.",
        "source_title": "Hazan & Shaver 1987",
        "source_url": "https://doi.org/10.1037/x",
        "source_type": "peer-reviewed study",
        "confidence": "high",
        "usage": "supported",
    }
    llm = FakeLLM({"notes": [note]})
    await run_evidence_research(db, job, llm, verifier=verifier)

    events = _events(db, job)
    assert len(events) == 1
    assert events[0].step == "evidence_research"
    assert events[0].provider == "exa"
    assert events[0].amount == Decimal("0.020000")
    assert events[0].detail == "https://doi.org/10.1037/x"


# --------------------------------------------------------------- images
class FakeImageProvider(ImageProvider):
    def __init__(self, settings, *, cost=0.25):
        self._settings = settings
        self._cost = cost
        self.requests: list[ImageGenerationRequest] = []

    async def generate(self, request: ImageGenerationRequest) -> GeneratedImage:
        self.requests.append(request)
        path, mime = save_image_bytes(
            request.job_id or "nojob",
            PNG_1X1_BYTES,
            request.filename,
            settings=self._settings,
        )
        return GeneratedImage(
            local_path=str(path),
            filename=request.filename,
            mime_type=mime,
            prompt=request.prompt,
            provider="fake-image-model",
            provider_request_id=f"fake-{request.filename}",
            provider_cost=self._cost,
        )

    async def health_check(self) -> bool:
        return True


def _seed_image_plan(session, job) -> ImageRow:
    session.add(
        ArticleVersionRow(
            job_id=job.id,
            version=1,
            stage="revision",
            title="R2 Cost Guide",
            body_markdown="## Intro\n\nBody text.\n",
            seo_title="seo",
            meta_description="meta",
            slug="r2-cost-guide",
        )
    )
    hero = ImageRow(
        job_id=job.id,
        role="hero",
        sort_order=0,
        purpose="anchor",
        section_heading=None,
        insertion_marker=None,
        prompt="hero prompt",
        filename="hero.webp",
        alt_text="Hero alt",
        aspect_ratio="16:9",
        provider="fake-image-model",
        local_path=None,
    )
    session.add(hero)
    session.commit()
    return hero


async def test_r_m02_image_regeneration_appends_charge_reuse_is_zero(db, tmp_path):
    job = _job(db)
    settings = _settings(tmp_path)
    hero = _seed_image_plan(db, job)

    provider = FakeImageProvider(settings, cost=0.25)
    await run_image_generation(db, job, provider, settings=settings)
    events = _events(db, job)
    assert len(events) == 1
    assert events[0].step == "image_generate"
    assert events[0].kind == "charged"
    assert events[0].amount == Decimal("0.250000")

    # Second run: the valid local file is reused -> zero-cost 'reused' event
    # and NO new paid generation.
    await run_image_generation(db, job, provider, settings=settings)
    events = _events(db, job)
    # (SQLite timestamps have second precision, so compare as a multiset.)
    assert sorted(e.kind for e in events) == ["charged", "reused"]
    assert all(
        e.amount == Decimal("0.000000")
        for e in events
        if e.kind == "reused"
    )
    assert len(provider.requests) == 1  # only the first run generated

    # Corrupt the file -> the next run regenerates and APPENDS a new charge
    # (the row's provider_cost is overwritten, the ledger is not).
    from pathlib import Path

    Path(hero.local_path).write_bytes(b"not-an-image")
    await run_image_generation(db, job, provider, settings=settings)
    events = _events(db, job)
    kinds = sorted(e.kind for e in events)
    assert kinds == ["charged", "charged", "reused"]
    assert total_cost(db, job.id) == Decimal("0.500000")


async def test_r_m02_image_planning_reset_keeps_image_cost_history(db, tmp_path):
    """A reset from step 14 deletes the ImageRow (and its provider_cost) but
    the ledger keeps the paid generation."""
    job = _job(db)
    settings = _settings(tmp_path)
    _seed_image_plan(db, job)
    await run_image_generation(
        db, job, FakeImageProvider(settings), settings=settings
    )
    before = _events(db, job)
    assert len(before) == 1

    checkpoints.reset_from_step(db, job, 14)
    db.commit()
    assert db.scalars(select(ImageRow).where(ImageRow.job_id == job.id)).first() is None
    assert len(_events(db, job)) == 1
    assert total_cost(db, job.id) == Decimal("0.250000")
