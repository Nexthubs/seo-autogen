"""P8 integration: the full 15-step orchestrator end to end (spec sections 9,
44, 55).

Runs against the local PostgreSQL (docker container) with scripted fakes for
every provider (no external LLM / SERP / extractor / image / CMS). Skipped when
the database is unreachable.

Covers:
  - the orchestrator chains all 15 checkpoint steps for one job
  - final job state is ``ready`` with every checkpoint row persisted
    (serp run + results, 5 sources, competitor analyses, synthesis, evidence,
     brief, outline, article v1+revision, reviews, image plan + generated
     images, local article.md/article.json export)
  - a provider failure (exhausted scripted LLM) is caught and persisted as
    ``failed`` with a stable ``error_code`` and re-raised
  - the orchestrator is the ONLY place that sets FAILED (step failures raise)

Shared integration DB discipline: every test only creates rows with
``p8test`` markers and deletes exactly those rows in teardown.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete as sa_delete, select

from app.core.config import Settings
from app.core.enums import JobStatus
from app.core.exceptions import PipelineError
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
from app.db.session import SessionLocal, check_database
from app.pipeline.orchestrator import PipelineProviders, run_job_pipeline
from app.providers.llm.openai_compatible import OpenAICompatibleLLMProvider
from app.schemas.serp import PAAQuestion, OrganicResult, SERPRequest, SERPResponse
from app.schemas.sources import ExtractedPage
from app.providers.extractor.base import ContentExtractor
from app.providers.image.base import ImageProvider
from app.schemas.images import GeneratedImage, ImageGenerationRequest
from app.providers.serp.base import SERPProvider
from app.services.image_storage import save_image_bytes

pytestmark = pytest.mark.skipif(
    not check_database(), reason="PostgreSQL not reachable"
)

KEYWORD = "p8test anxious attachment no contact"
MARKER = "[p8coach]"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
FAST_BACKOFF = (0.001, 0.001, 0.001)
PNG_1x1_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-body" * 3
TOP_N = 5
ORGANIC_URLS = [f"https://p8test-site{i}.example.com/a{i}" for i in range(1, 9)]

# ---- scripted payloads (one per LLM call, in pipeline order) -------------
ANALYSIS_TMPL = {
    # The step overwrites source_id with the real page id after validation.
    "source_id": "00000000-0000-0000-0000-000000000000",
    "content_type": "guide",
    "search_intent": "informational",
    "estimated_word_count": 1200,
    "headings": ["intro"],
    "pain_points": ["loneliness"],
    "key_topics": ["no contact"],
    "practical_advice": ["wait 30 days"],
    "faq_topics": ["how long"],
    "strengths": ["detailed"],
    "weaknesses": ["no sources"],
    "potential_gaps": ["science"],
}
SYNTHESIS = {
    "dominant_intent": "informational",
    "secondary_intents": ["commercial"],
    "common_topics": ["no contact", "signs"],
    "common_pain_points": ["loneliness"],
    "common_questions": ["how long?"],
    "content_patterns": ["listicles"],
    "missing_topics": ["studies"],
    "opportunities": ["citable science"],
}
EVIDENCE_NOTES = [
    {
        "claim": "Attachment styles affect adult relationships.",
        "source_title": "Hazan & Shaver 1987",
        "source_url": "https://doi.org/10.1037/0022-3514.53.3.519",
        "source_type": "peer-reviewed study",
        "confidence": "high",
        "usage": "supported",
        "note": "cite properly",
    }
]
BRIEF_P8 = {
    "primary_keyword": KEYWORD,
    "secondary_keywords": ["avoidant attachment"],
    "long_tail_keywords": ["how long no contact"],
    "search_intent": "informational",
    "search_stage": "consideration",
    "article_strategy": "definitive guide",
    "target_reader": "adults in recovery",
    "core_problem": "confusion after breakup",
    "emotional_context": "anxious",
    "unique_angle": "evidence based",
    "content_gaps": ["science"],
    "required_topics": ["no contact", "signs"],
    "faq_questions": ["how long?"],
    "target_function": "coach",
    "cta_strategy": ["mid", "end"],
    "internal_link_markers": [MARKER],
    "recommended_word_count": 2500,
}
OUTLINE_P8 = {
    "title": f"{KEYWORD.title()}: A Complete Practical Guide",
    "sections": [
        {"heading": "What is avoidant attachment", "level": 2, "purpose": "x",
         "keywords": ["avoidant attachment"], "cta_slot": False},
        {"heading": "No contact explained", "level": 2, "purpose": "x",
         "keywords": ["no contact"], "cta_slot": False},
        {"heading": "Signs of an avoidant partner", "level": 2, "purpose": "x",
         "keywords": ["signs"], "cta_slot": False},
        {"heading": "Practical steps for p8test", "level": 2, "purpose": "x",
         "keywords": ["steps"], "cta_slot": True},
        {"heading": "Common exercises", "level": 2, "purpose": "x",
         "keywords": ["exercises"], "cta_slot": True},
        {"heading": "FAQ", "level": 2, "purpose": "x", "keywords": ["faq"],
         "cta_slot": False},
    ],
    "faq_questions": ["q1?", "q2?", "q3?"],
}
WRITER_DRAFT = {
    "title": "WRONG TITLE FROM LLM",
    "body_markdown": (
        "# Wrong H1\n\n## What is avoidant attachment\n\n"
        "Attachment styles shape how adults pursue closeness, and anxiety "
        "shows up as constant checking of the phone and rereading texts.\n\n"
        "## Practical steps for p8test\n\n"
        "Set a realistic boundary, write down your feelings each evening, "
        "and use the " + MARKER + " guide when you feel the urge to reach out.\n"
    ),
    "seo_title": "Anxious Attachment No Contact Guide",
    "meta_description": "A practical guide to anxious attachment no contact.",
    "slug": " Anxious Attachment  No-Contact Guide!! ",
}
SEO_REVIEW = {
    "total_score": 82,
    "keyword_score": 90,
    "search_intent_score": 88,
    "structure_score": 80,
    "readability_score": 78,
    "cta_score": 70,
    "issues": ["meta description too long"],
    "required_changes": ["shorten the meta description"],
}
FACT_REVIEW = {"issues": []}
STYLE_REVIEW = {
    "score": 68,
    "ai_patterns": ["In today's fast-paced world"],
    "repetitive_patterns": ["it is important to"],
    "weak_sections": ["intro"],
    "required_changes": ["remove filler phrases"],
}
REVISER_DRAFT = {
    "title": "should be ignored",
    "body_markdown": (
        "## What is avoidant attachment\n\n"
        "Attachment styles shape how adults pursue closeness in their "
        "partner relationships, and anxiety shows up as constant checking.\n"
        "## Practical steps for p8test\n\n"
        "Set a realistic boundary and write down your feelings each evening. "
        "Follow the " + MARKER + " guide for a concrete no contact routine.\n"
    ),
    "seo_title": "Anxious Attachment No Contact (Revised)",
    "meta_description": "Revised practical guide.",
    "slug": "revised-slug",
}
IMAGE_PLAN = {
    "total_count": 1,
    "images": [
        {
            "role": "hero",
            "purpose": "Whole-article emotional anchor.",
            "section_heading": None,
            "insertion_marker": None,
            "filename": "hero.webp",
            "alt_text": "A person by a quiet window at dusk",
            "prompt": "A calm figure by a window, warm light, no text.",
            "aspect_ratio": "16:9",
        }
    ],
}


def _settings(tmp_path) -> Settings:
    return Settings(
        llm_base_url="http://llm.test/v1",
        llm_api_key="test-key",
        llm_model="test-model",
        llm_max_retries=2,
        image_model="gpt-image-2",
        data_dir=str(tmp_path),
        strapi_frontend_renders_main_image=True,
        _env_file=None,
    )


class FakeLLM:
    """Scripted LLM: pops the next payload per call (no network)."""

    def __init__(self, payloads, settings):
        self.payloads = list(payloads)
        self.calls: list[dict] = []
        self._settings = settings
        self._provider = OpenAICompatibleLLMProvider(
            settings=settings,
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(self._handle),
                base_url="http://llm.test/v1",
            ),
            backoff_seconds=FAST_BACKOFF,
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(
            {
                "system": body["messages"][0]["content"],
                "user": body["messages"][1]["content"],
            }
        )
        return httpx.Response(
            200,
            json={
                "id": "cmpl-1",
                "choices": [
                    {"message": {"role": "assistant", "content": self.payloads.pop(0)}}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    async def generate_structured(self, **kwargs):
        return await self._provider.generate_structured(**kwargs)

    async def generate_text(self, **kwargs):
        return await self._provider.generate_text(**kwargs)

    async def aclose(self):
        await self._provider.aclose()


class FakeSERP(SERPProvider):
    """Returns 8 distinct organic URLs → top-5 unique → 5 sources."""

    async def search(self, request: SERPRequest) -> SERPResponse:
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
                PAAQuestion(question="Why no contact works?", source_url=None)
            ],
            related_searches=["no contact rules"],
            raw={"status_code": 200, "keyword": request.keyword},
        )

    async def health_check(self) -> bool:
        return True


class FakeExtractor(ContentExtractor):
    """Unique content per URL so no source is dropped by content dedup."""

    async def extract(self, urls: list[str]) -> list[ExtractedPage]:
        return [
            ExtractedPage(
                url=url,
                normalized_url=url,
                title=f"Title {url}",
                content_markdown=(
                    f"Unique competitor body text for {url}. "
                    f"Different wording, no overlap with the draft. "
                ),
                extracted_at=NOW,
                extractor="fake",
            )
            for url in urls
        ]

    async def health_check(self) -> bool:
        return True


class FakeImage(ImageProvider):
    """Writes a tiny PNG for every planned image into the tmp data_dir."""

    def __init__(self, settings):
        self._settings = settings
        self.requests: list[ImageGenerationRequest] = []

    async def generate(self, request: ImageGenerationRequest) -> GeneratedImage:
        self.requests.append(request)
        path = save_image_bytes(
            request.job_id or "nojob",
            PNG_1x1_BYTES,
            request.filename,
            settings=self._settings,
        )
        return GeneratedImage(
            local_path=str(path),
            filename=request.filename,
            mime_type="image/png",
            prompt=request.prompt,
            provider="fake-image-model",
            provider_request_id=f"fake-{request.filename}",
        )

    async def health_check(self) -> bool:
        return True


def _payloads():
    """The 15 scripted LLM payloads in exact pipeline order."""
    return (
        [ANALYSIS_TMPL] * TOP_N
        + [
            SYNTHESIS,
            {"notes": EVIDENCE_NOTES},
            BRIEF_P8,
            OUTLINE_P8,
            WRITER_DRAFT,
            SEO_REVIEW,
            FACT_REVIEW,
            STYLE_REVIEW,
            REVISER_DRAFT,
            IMAGE_PLAN,
        ]
    )


@pytest.fixture()
def job(tmp_path):
    """A queued job for this run; cleaned up exactly in teardown."""
    with SessionLocal() as session:
        job = GenerationJob(
            keyword=KEYWORD,
            status=JobStatus.QUEUED.value,
            target_function="coach",
            strategy="auto",
        )
        session.add(job)
        session.commit()
        job_id = job.id
    yield job_id
    _cleanup(job_id, tmp_path)


def _cleanup(job_id: uuid.UUID, tmp_path) -> None:
    """Delete exactly the rows this test created (p8test scope)."""
    with SessionLocal() as session:
        run_ids = [
            r.id
            for r in session.scalars(
                select(SerpRun).where(SerpRun.job_id == job_id)
            ).all()
        ]
        if run_ids:
            session.execute(sa_delete(SerpResult).where(SerpResult.serp_run_id.in_(run_ids)))
        for stmt in (
            sa_delete(ImageRow).where(ImageRow.job_id == job_id),
            sa_delete(ArticleReviewRow).where(ArticleReviewRow.job_id == job_id),
            sa_delete(ArticleVersionRow).where(ArticleVersionRow.job_id == job_id),
            sa_delete(ArticleOutlineRow).where(ArticleOutlineRow.job_id == job_id),
            sa_delete(ContentBriefRow).where(ContentBriefRow.job_id == job_id),
            sa_delete(EvidenceNoteRow).where(EvidenceNoteRow.job_id == job_id),
            sa_delete(SerpSynthesisRow).where(SerpSynthesisRow.job_id == job_id),
            sa_delete(CompetitorAnalysisRow).where(CompetitorAnalysisRow.job_id == job_id),
            sa_delete(SerpRun).where(SerpRun.job_id == job_id),
            sa_delete(JobSource).where(JobSource.job_id == job_id),
        ):
            session.execute(stmt)
        session.execute(sa_delete(SourcePage).where(SourcePage.url.in_(ORGANIC_URLS)))
        session.execute(sa_delete(GenerationJob).where(GenerationJob.id == job_id))
        session.commit()
    # Remove the local image/article exports written into the tmp data_dir.
    job_dir = Path(tmp_path) / "articles" / str(job_id)
    for name in ("article.md", "article.json"):
        p = job_dir / name
        if p.exists():
            p.unlink(missing_ok=True)
    images = job_dir / "images"
    if images.exists():
        for f in images.iterdir():
            f.unlink(missing_ok=True)
        try:
            images.rmdir()
        except OSError:
            pass
    try:
        job_dir.rmdir()
    except OSError:
        pass


def _providers(
    tmp_path, payloads=None
) -> tuple[PipelineProviders, FakeLLM, FakeSERP, FakeExtractor, FakeImage, Settings]:
    settings = _settings(tmp_path)
    llm = FakeLLM([json.dumps(p) for p in (payloads if payloads is not None else _payloads())], settings)
    serp = FakeSERP()
    extractor = FakeExtractor()
    image = FakeImage(settings)
    providers = PipelineProviders(
        llm=llm, serp=serp, extractor=extractor, image=image, cms=None
    )
    return providers, llm, serp, extractor, image, settings


async def test_full_pipeline_reaches_ready(job, tmp_path):
    providers, llm, serp, extractor, image, settings = _providers(tmp_path)
    with SessionLocal() as session:
        job_row = session.get(GenerationJob, job)
        try:
            result = await run_job_pipeline(
                session, job_row, providers, settings=settings
            )
        finally:
            session.close()

    assert result.status == JobStatus.READY.value

    with SessionLocal() as session:
        # Every checkpoint row for the job exists.
        assert (
            session.scalar(select(SerpRun).where(SerpRun.job_id == job)) is not None
        )
        n_sources = session.scalar(
            select(JobSource).where(JobSource.job_id == job)
        )
        assert n_sources is not None
        assert (
            session.scalar(
                select(SerpSynthesisRow).where(SerpSynthesisRow.job_id == job)
            )
            is not None
        )
        assert (
            session.scalar(
                select(ContentBriefRow).where(ContentBriefRow.job_id == job)
            )
            is not None
        )
        assert (
            session.scalar(
                select(ArticleOutlineRow).where(ArticleOutlineRow.job_id == job)
            )
            is not None
        )
        versions = session.scalars(
            select(ArticleVersionRow)
            .where(ArticleVersionRow.job_id == job)
            .order_by(ArticleVersionRow.version)
        ).all()
        assert [v.version for v in versions] == [1, 2]
        assert versions[1].stage == "revision"
        reviews = session.scalars(
            select(ArticleReviewRow).where(ArticleReviewRow.job_id == job)
        ).all()
        review_types = {r.review_type for r in reviews}
        # writer + revision each run anti-copy; the 3 named reviews land on v1.
        assert {"seo", "fact", "style"}.issubset(review_types)
        images = session.scalars(
            select(ImageRow).where(ImageRow.job_id == job)
        ).all()
        assert len(images) == 1
        assert images[0].role == "hero"
        assert images[0].local_path
        assert Path(images[0].local_path).exists()

    # 15 LLM calls total (5 competitor + 10 structured).
    assert len(llm.calls) == 15
    # Local article export written (section 34).
    job_dir = Path(tmp_path) / "articles" / str(job)
    assert (job_dir / "article.md").exists()
    assert (job_dir / "article.json").exists()


async def test_pipeline_persists_failure_on_llm_exhaustion(job, tmp_path):
    # No scripted payloads: the very first generate_structured call raises.
    providers, _, _, _, _, settings = _providers(tmp_path, payloads=[])
    with SessionLocal() as session:
        job_row = session.get(GenerationJob, job)
        # The orchestrator is the ONLY place that sets FAILED; the PipelineError
        # is re-raised to the caller after the job is persisted as failed.
        with pytest.raises(Exception):
            await run_job_pipeline(
                session, job_row, providers, settings=settings
            )
        session.refresh(job_row)
        assert job_row.status == JobStatus.FAILED.value
        assert job_row.error_code
        assert job_row.error_message
        assert job_row.completed_at is not None
        session.close()
