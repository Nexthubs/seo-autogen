"""P9-B1 unit: cost tracking (SEO-AUTO-DEV-SPEC.md section 54).

Covers the per-provider cost plumbing (DataForSEO credits, Exa
costDollars, image ``cost`` field) and the LLM usage metering: the
OpenAI-compatible provider's token accumulation (a structured-output
repair counting as ONE logical call), the ``MeteredLLMProvider`` row
recording, and ``reset_from_step`` deleting the retried steps' usage
rows. All on SQLite / in-memory HTTP — no integration database needed.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models  # noqa: F401 - register all models on Base
from app.db.base import Base
from app.db.models.job import GenerationJob
from app.db.models.llm_usage import LLMUsageRow
from app.core.config import Settings
from app.core.enums import JobStatus
from app.pipeline import checkpoints
from app.providers.extractor.exa import ExaContentExtractor
from app.providers.image.openai_image import _extract_cost
from app.providers.llm.openai_compatible import OpenAICompatibleLLMProvider
from app.providers.serp.dataforseo import DataForSEOSERPProvider
from app.schemas.serp import SERPRequest
from app.services.llm_metering import MeteredLLMProvider


def _settings() -> Settings:
    return Settings(
        llm_base_url="http://llm.test/v1",
        llm_api_key="test-key",
        llm_model="test-model",
        exa_api_key="exa-key",
        dataforseo_base_url="https://api.dataforseo.test",
        dataforseo_login="u",
        dataforseo_password="p",
        image_api_key="img-key",
        image_base_url="http://img.test/v1",
        image_model="gpt-image-2",
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


def _make_job(session) -> GenerationJob:
    job = GenerationJob(
        keyword="p9b1 cost",
        status=JobStatus.QUEUED.value,
        target_function="coach",
        strategy="auto",
    )
    session.add(job)
    session.commit()
    return job


def _llm(handler) -> OpenAICompatibleLLMProvider:
    return OpenAICompatibleLLMProvider(
        settings=_settings(),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://llm.test/v1",
        ),
        backoff_seconds=(0.0, 0.0, 0.0),
    )


def _json_response(content, usage) -> httpx.Response:
    payload: dict = {
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }
    if usage is not None:
        payload["usage"] = usage
    return httpx.Response(200, json=payload)


# ---------------------------------------------------------------------------
# OpenAI-compatible provider: usage accumulation
# ---------------------------------------------------------------------------
class TestLLMUsageAccumulation:
    async def test_accumulates_usage_and_resets(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(
                "ok", {"prompt_tokens": 3, "completion_tokens": 5}
            )

        provider = _llm(handler)
        assert provider.take_usage() is None

        provider.begin_usage()
        await provider.generate_text(system_prompt="s", user_prompt="u")
        assert provider.take_usage() == (3, 5)

        # A new logical call starts from a clean accumulator.
        provider.begin_usage()
        assert provider.take_usage() is None

    async def test_no_usage_block_yields_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response("ok", None)

        provider = _llm(handler)
        provider.begin_usage()
        await provider.generate_text(system_prompt="s", user_prompt="u")
        assert provider.take_usage() is None

    async def test_repair_attempts_accumulate_into_one_call(self):
        """A structured call that needs one repair makes TWO HTTP calls;
        their usage blocks accumulate into a single logical-call total."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            # First attempt is invalid JSON (forces a repair prompt);
            # second is valid.
            content = "not json" if calls["n"] == 1 else '{"value": "ok"}'
            return _json_response(
                content, {"prompt_tokens": 6, "completion_tokens": 4}
            )

        provider = _llm(handler)

        class Out(BaseModel):
            value: str

        provider.begin_usage()
        result = await provider.generate_structured(
            system_prompt="s", user_prompt="u", response_model=Out
        )
        assert result.value == "ok"
        assert calls["n"] == 2
        # 2 HTTP calls * (6, 4) accumulated into one logical call.
        assert provider.take_usage() == (12, 8)


# ---------------------------------------------------------------------------
# MeteredLLMProvider: one llm_usage row per logical call
# ---------------------------------------------------------------------------
class TestMeter:
    async def test_records_row_per_call(self, db):
        job = _make_job(db)

        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(
                "ok", {"prompt_tokens": 2, "completion_tokens": 3}
            )

        meter = MeteredLLMProvider(_llm(handler), db, job.id)
        meter.current_step = "content_brief"
        await meter.generate_text(system_prompt="s", user_prompt="u")
        meter.current_step = "outline"
        await meter.generate_text(system_prompt="s", user_prompt="u")
        db.commit()

        rows = (
            db.scalars(select(LLMUsageRow).where(LLMUsageRow.job_id == job.id)).all()
        )
        assert len(rows) == 2
        assert {r.step for r in rows} == {"content_brief", "outline"}
        for r in rows:
            assert r.input_tokens == 2
            assert r.output_tokens == 3
            assert r.duration_ms >= 0
            assert r.model == "test-model"

    async def test_meter_works_over_generate_structured(self, db):
        job = _make_job(db)

        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(
                '{"value": "x"}',
                {"prompt_tokens": 7, "completion_tokens": 9},
            )

        meter = MeteredLLMProvider(_llm(handler), db, job.id)
        meter.current_step = "article_writer"

        class Out(BaseModel):
            value: str

        await meter.generate_structured(
            system_prompt="s", user_prompt="u", response_model=Out
        )
        db.commit()

        row = (
            db.scalars(select(LLMUsageRow).where(LLMUsageRow.job_id == job.id)).one()
        )
        assert row.step == "article_writer"
        assert (row.input_tokens, row.output_tokens) == (7, 9)

    async def test_records_prompt_provenance_when_set(self, db):
        """Spec 48 / audit M09: usage rows carry prompt_name / version / hash
        for calls made under a named prompt."""
        job = _make_job(db)

        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response("ok", None)

        meter = MeteredLLMProvider(_llm(handler), db, job.id, settings=_settings())
        meter.current_step = "content_brief"
        meter.current_prompt = ("content_brief", "1.0.0", "abc123")
        await meter.generate_text(system_prompt="s", user_prompt="u")
        meter.current_prompt = ("outline_repair", "2.1.0", "def456")
        meter.current_step = "outline"
        await meter.generate_text(system_prompt="s", user_prompt="u")
        db.commit()

        rows = {
            r.step: r
            for r in db.scalars(
                select(LLMUsageRow).where(LLMUsageRow.job_id == job.id)
            ).all()
        }
        assert (
            rows["content_brief"].prompt_name,
            rows["content_brief"].prompt_version,
            rows["content_brief"].prompt_hash,
        ) == ("content_brief", "1.0.0", "abc123")
        assert (
            rows["outline"].prompt_name,
            rows["outline"].prompt_version,
            rows["outline"].prompt_hash,
        ) == ("outline_repair", "2.1.0", "def456")
        for r in rows.values():
            assert r.model == "test-model"

    async def test_prompt_columns_null_when_unset(self, db):
        job = _make_job(db)

        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response("ok", None)

        meter = MeteredLLMProvider(_llm(handler), db, job.id)
        meter.current_step = "article_writer"
        await meter.generate_text(system_prompt="s", user_prompt="u")
        db.commit()

        row = (
            db.scalars(select(LLMUsageRow).where(LLMUsageRow.job_id == job.id)).one()
        )
        assert row.prompt_name is None
        assert row.prompt_version is None
        assert row.prompt_hash is None


# ---------------------------------------------------------------------------
# set_llm_prompt helper (audit M09)
# ---------------------------------------------------------------------------
class TestSetLLMPrompt:
    def test_sets_tuple_on_meter(self):
        from app.pipeline.steps._common import set_llm_prompt
        from app.services.prompt_service import PromptSpec

        class FakeMeter:
            current_prompt = None

        meter = FakeMeter()
        spec = PromptSpec(name="outline_generator", version="1.0.0",
                          content="c", prompt_hash="h1")
        set_llm_prompt(meter, spec)
        assert meter.current_prompt == ("outline_generator", "1.0.0", "h1")

    def test_noop_for_bare_provider_and_none(self):
        from app.pipeline.steps._common import set_llm_prompt
        from app.services.prompt_service import PromptSpec

        class Bare:  # no current_prompt attribute
            pass

        set_llm_prompt(Bare(), PromptSpec("n", "v", "c", "h"))  # no exception
        set_llm_prompt(Bare(), None)  # no exception


# ---------------------------------------------------------------------------
# DataForSEO cost passthrough
# ---------------------------------------------------------------------------
def _serp_raw(credits) -> dict:
    task = {
        "code": 200,
        "result": {
            "code": 200,
            "organic": [
                {"position": 1, "title": "t", "url": "https://a.example.com",
                 "domain": "a.example.com", "description": "d"}
            ],
        },
    }
    if credits is not None:
        task["credits"] = credits
    return {"status_code": 200, "tasks": [task]}


class TestDataForSEOCost:
    async def test_cost_extracted_and_carried(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_serp_raw(0.12))

        provider = DataForSEOSERPProvider(
            settings=_settings(),
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                base_url="http://serp.test",
            ),
            backoff_seconds=(0.0, 0.0, 0.0),
        )
        req = SERPRequest(keyword="k", location_code=2342, language_code="en")
        resp = await provider.search(req)
        assert resp.provider_cost == pytest.approx(0.12)

    async def test_cost_none_when_absent(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_serp_raw(None))

        provider = DataForSEOSERPProvider(
            settings=_settings(),
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                base_url="http://serp.test",
            ),
            backoff_seconds=(0.0, 0.0, 0.0),
        )
        req = SERPRequest(keyword="k", location_code=2342, language_code="en")
        resp = await provider.search(req)
        assert resp.provider_cost is None


# ---------------------------------------------------------------------------
# Exa cost parse
# ---------------------------------------------------------------------------
def _exa_raw(with_cost: bool) -> dict:
    raw: dict = {
        "results": [
            {"id": "https://a.example.com", "url": "https://a.example.com",
             "text": "hello world body", "title": "A"}
        ],
        "statuses": [{"id": "https://a.example.com", "status": "success"}],
    }
    if with_cost:
        raw["costDollars"] = {"total": 0.01}
    return raw


class TestExaCost:
    async def test_cost_dollars_attached_to_pages(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_exa_raw(True))

        provider = ExaContentExtractor(
            settings=_settings(),
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                base_url="http://exa.test",
            ),
        )
        pages = await provider.extract(["https://a.example.com"])
        assert len(pages) == 1
        assert pages[0].provider_cost == pytest.approx(0.01)

    async def test_no_cost_block_yields_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_exa_raw(False))

        provider = ExaContentExtractor(
            settings=_settings(),
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                base_url="http://exa.test",
            ),
        )
        pages = await provider.extract(["https://a.example.com"])
        assert pages[0].provider_cost is None


# ---------------------------------------------------------------------------
# Image cost parse helper
# ---------------------------------------------------------------------------
class TestImageCost:
    def test_extract_cost_shapes(self):
        assert _extract_cost({"cost": 0.04}) == pytest.approx(0.04)
        assert _extract_cost({"cost": 1}) == pytest.approx(1.0)
        assert _extract_cost({"usage": {"cost": 0.02}}) == pytest.approx(0.02)
        assert _extract_cost({}) is None
        assert _extract_cost({"cost": "free"}) is None
        assert _extract_cost({"cost": True}) is None


# ---------------------------------------------------------------------------
# reset_from_step deletes the retried steps' llm_usage rows
# ---------------------------------------------------------------------------
class TestResetDeletesUsage:
    def test_reset_step8_keeps_earlier_deletes_later(self, db):
        job = _make_job(db)
        for step in ("content_brief", "outline", "article_writer"):
            db.add(LLMUsageRow(job_id=job.id, step=step, input_tokens=1,
                               output_tokens=1, duration_ms=10))
        # An earlier step's usage that must SURVIVE a step-8 reset.
        db.add(LLMUsageRow(job_id=job.id, step="competitor_analysis",
                           input_tokens=1, output_tokens=1, duration_ms=10))
        db.commit()

        checkpoints.reset_from_step(db, job, 8)
        db.commit()

        remaining = {
            r.step
            for r in db.scalars(
                select(LLMUsageRow).where(LLMUsageRow.job_id == job.id)
            ).all()
        }
        # content_brief (7) and competitor_analysis (4) survive (< 8);
        # outline (8) and article_writer (9) are deleted.
        assert remaining == {"content_brief", "competitor_analysis"}

    def test_full_reset_deletes_all_usage(self, db):
        job = _make_job(db)
        db.add(LLMUsageRow(job_id=job.id, step="competitor_analysis",
                           input_tokens=1, output_tokens=1, duration_ms=10))
        db.add(LLMUsageRow(job_id=job.id, step="content_brief",
                           input_tokens=1, output_tokens=1, duration_ms=10))
        db.commit()

        checkpoints.reset_from_step(db, job, 1)
        db.commit()

        remaining = db.scalars(
            select(LLMUsageRow).where(LLMUsageRow.job_id == job.id)
        ).all()
        assert remaining == []
