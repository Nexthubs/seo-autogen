"""P9-A integration: core reliability on the real PostgreSQL (spec sections 9,
14.2, 43.3, 44).

Same harness discipline as ``test_p8_pipeline.py``: scripted fakes for every
provider, rows scoped to ``p9test`` markers, exact-teardown cleanup.

Covers the P9-A reliability behaviours end to end:

  - **Retry by step** (spec 44/43.3): a job that fails at the competitor
    analysis step is re-run with ``retry_step=14`` — earlier checkpoint rows
    (SERP run, sources, analyses, brief, outline, writer draft) survive, only
    the image rows are rebuilt, and exactly the 6 LLM calls from the writer
    step onward are re-made.
  - **Resume from checkpoint** (spec 9): a job that fails with the LLM
    exhausted at step 4 reports ``first_incomplete_step == 4`` and
    ``resume_from_step=4`` re-runs from exactly that step (no deletion) until
    ``ready``.
  - **In-flight cancellation** (spec 44/43.3): a job cancelled from the web
    while a step is in flight stops at the next step boundary, keeps the
    ``cancelled`` status (never flipped to ``ready``/``failed``) and does not
    make any further LLM calls.
  - **Force source refresh** (spec 14.2): a plain re-run of step 3 serves
    every URL from the fresh TTL cache (zero extractor calls) while
    ``force_source_refresh=True`` re-extracts every URL.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete as sa_delete, func, select

from app.core.config import Settings
from app.core.enums import JobStatus
from app.core.exceptions import PipelineError
from app.db.models.article import ArticleReviewRow, ArticleVersionRow
from app.db.models.images import ImageRow
from app.db.models.job import GenerationJob
from app.db.models.keyword import Keyword, KeywordCluster
from app.db.models.research import (
    ArticleOutlineRow,
    CompetitorAnalysisRow,
    ContentBriefRow,
    EvidenceNoteRow,
    SerpSynthesisRow,
)
from app.db.models.serp import SerpResult, SerpRun
from app.db.models.source import JobSource, SourcePage
from app.db.models.llm_usage import LLMUsageRow
from app.db.session import SessionLocal, check_database
from app.pipeline import checkpoints
from app.pipeline.orchestrator import PipelineProviders, run_job_pipeline
from app.pipeline.steps.serp_search import run_serp_search
from app.pipeline.steps.source_extract import run_source_extract
from app.providers.llm.openai_compatible import OpenAICompatibleLLMProvider
from app.services.source_cache import SourceCache
from app.providers.serp.base import SERPProvider
from app.schemas.serp import PAAQuestion, OrganicResult, SERPRequest, SERPResponse
from app.schemas.sources import ExtractedPage
from app.providers.extractor.base import ContentExtractor
from app.providers.image.base import ImageProvider
from app.schemas.images import GeneratedImage, ImageGenerationRequest
from app.services.image_storage import save_image_bytes

pytestmark = pytest.mark.skipif(
    not check_database(), reason="PostgreSQL not reachable"
)

KEYWORD = "p9test anxious attachment recovery"
MARKER = "[p9coach]"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
FAST_BACKOFF = (0.001, 0.001, 0.001)
PNG_1x1_BYTES = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xe0\x12\x91\x03\x00\x00h\x00=T\x08\xa3\xf7\x00\x00\x00\x00IEND\xaeB`\x82'
TOP_N = 5
ORGANIC_URLS = [f"https://p9test-site{i}.example.com/a{i}" for i in range(1, 9)]
# H09: evidence_research (step 6) verifies its single note's source URL via the
# independent extractor channel, adding exactly one extract() call to any run
# that reaches step 6 (full runs and retry_step<=6; NOT retry_step=14).
EVIDENCE_SOURCE_URL = "https://doi.org/10.1037/0022-3514.53.3.519"

# ---- scripted payloads (one per LLM call, in pipeline order) -------------
ANALYSIS_TMPL = {
    # The step overwrites source_id with the real page id after validation.
    "source_id": "00000000-0000-0000-0000-000000000000",
    "content_type": "guide",
    "search_intent": "informational",
    "estimated_word_count": 1200,
    "headings": ["intro"],
    "pain_points": ["loneliness"],
    "key_topics": ["recovery"],
    "practical_advice": ["wait 30 days"],
    "faq_topics": ["how long"],
    "strengths": ["detailed"],
    "weaknesses": ["no sources"],
    "potential_gaps": ["science"],
}
SYNTHESIS = {
    "dominant_intent": "informational",
    "secondary_intents": ["commercial"],
    "common_topics": ["recovery", "signs"],
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
BRIEF_P9 = {
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
    "required_topics": ["recovery", "signs"],
    "faq_questions": ["how long?"],
    "target_function": "coach",
    "cta_strategy": ["mid", "end"],
    "internal_link_markers": [MARKER],
    "recommended_word_count": 2500,
}
OUTLINE_P9 = {
    "title": f"{KEYWORD.title()}: A Complete Practical Guide",
    "sections": [
        {"heading": "What is avoidant attachment", "level": 2, "purpose": "x",
         "keywords": ["avoidant attachment"], "cta_slot": False},
        {"heading": "Recovery explained", "level": 2, "purpose": "x",
         "keywords": ["recovery"], "cta_slot": False},
        {"heading": "Signs of an avoidant partner", "level": 2, "purpose": "x",
         "keywords": ["signs"], "cta_slot": False},
        {"heading": "Practical steps for p9test", "level": 2, "purpose": "x",
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
        "## Practical steps for p9test\n\n"
        "Set a realistic boundary, write down your feelings each evening, "
        "and use the " + MARKER + " guide when you feel the urge to reach out.\n"
    ),
    "seo_title": "Anxious Attachment Recovery Guide",
    "meta_description": "A practical guide to anxious attachment recovery.",
    "slug": " Anxious Attachment  Recovery Guide!! ",
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
    # DoD-compliant final body (section 63 / writer contract): no H1,
    # CTA slot section, and a ## FAQ section.
    "body_markdown": (
        "## What is avoidant attachment\n\n"
        "Attachment styles shape how adults pursue closeness in their "
        "partner relationships, and anxiety shows up as constant checking.\n"
        "## Practical steps for p9test\n\n"
        "Set a realistic boundary and write down your feelings each evening. "
        "Follow the " + MARKER + " guide for a concrete recovery routine.\n"
        "## FAQ\n\n"
        "### q1?\n\n"
        "A short, original p9 answer to the first question.\n\n"
        "### q2?\n\n"
        "A short, original p9 answer to the second question.\n\n"
        "### q3?\n\n"
        "A short, original p9 answer to the third question.\n"
    ),
    "seo_title": "Anxious Attachment Recovery (Revised)",
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


def _payloads() -> list[dict]:
    """The 15 scripted LLM payloads in exact pipeline order."""
    return (
        [ANALYSIS_TMPL] * TOP_N
        + [
            SYNTHESIS,
            {"notes": EVIDENCE_NOTES},
            BRIEF_P9,
            OUTLINE_P9,
            WRITER_DRAFT,
            SEO_REVIEW,
            FACT_REVIEW,
            STYLE_REVIEW,
            REVISER_DRAFT,
            IMAGE_PLAN,
        ]
    )


def _payloads_from(start_step: int) -> list[dict]:
    """The payloads a run starting at ``start_step`` will consume.

    The 15-entry chain is indexed so that payload index == step number for
    steps 5..14 (idx 0-4 are the ``TOP_N`` competitor-analysis calls of step
    4; idx 5..14 are steps 5..14; step 15 makes no LLM call). A run starting
    at ``start_step`` consumes steps ``start_step..15``:

    - ``start_step <= 4`` → steps 1..15 all reach the LLM (step 4 makes 5
      calls) → the full 15-payload chain.
    - ``start_step >= 5`` → steps 5..14 each make one call → the suffix
      ``_payloads()[start_step:]`` (indices 5..14).
    """
    if start_step <= 4:
        return _payloads()
    return _payloads()[start_step:]


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
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
        # Exhaustion is a server fault (500), NOT a successful call: the
        # caller's retry loop re-tries it (3 attempts) and every attempt
        # returns this, so ``self.calls`` never grows for an exhausted step.
        if not self.payloads:
            return httpx.Response(500, json={"error": "scripted payloads exhausted"})
        content = self.payloads.pop(0)
        # Record the call only on the 200 path, so ``len(self.calls)`` is
        # exactly the number of successful logical LLM calls.
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
                    {"message": {"role": "assistant", "content": content}}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    async def generate_structured(self, **kwargs):
        return await self._provider.generate_structured(**kwargs)

    async def generate_text(self, **kwargs):
        return await self._provider.generate_text(**kwargs)

    def begin_usage(self):
        self._provider.begin_usage()

    def take_usage(self):
        return self._provider.take_usage()

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
                PAAQuestion(question="Why recovery works?", source_url=None)
            ],
            related_searches=["recovery rules"],
            raw={"status_code": 200, "keyword": request.keyword},
            provider_cost=1.5,
        )

    async def health_check(self) -> bool:
        return True


class FakeExtractor(ContentExtractor):
    """Unique content per URL so no source is dropped by content dedup.

    Records every per-URL call so cache-hit vs forced re-extract can be
    asserted (spec 14.2).
    """

    def __init__(self):
        self.extract_calls: list[list[str]] = []

    async def extract(self, urls: list[str]) -> list[ExtractedPage]:
        self.extract_calls.append(list(urls))
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
                provider_cost=0.05,
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
        path, mime = save_image_bytes(
            request.job_id or "nojob",
            PNG_1x1_BYTES,
            request.filename,
            settings=self._settings,
        )
        return GeneratedImage(
            local_path=str(path),
            filename=request.filename,
            # M07: .webp filenames are transcoded to real WebP by storage.
            mime_type=mime,
            prompt=request.prompt,
            provider="fake-image-model",
            provider_request_id=f"fake-{request.filename}",
            provider_cost=0.10,
        )

    async def health_check(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
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
    """Delete exactly the rows this test created (p9test scope)."""
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
            sa_delete(CompetitorAnalysisRow).where(
                CompetitorAnalysisRow.job_id == job_id
            ),
            sa_delete(SerpRun).where(SerpRun.job_id == job_id),
            sa_delete(JobSource).where(JobSource.job_id == job_id),
        ):
            session.execute(stmt)
        session.execute(sa_delete(SourcePage).where(SourcePage.url.in_(ORGANIC_URLS)))
        session.execute(sa_delete(GenerationJob).where(GenerationJob.id == job_id))
        session.commit()
    # Remove the local image/article exports written into the tmp data_dir.
    job_dir = Path(tmp_path) / "articles" / str(job_id)
    for name in (
        "article.md",
        "article.json",
        "content-brief.json",
        "outline.json",
        "serp.json",
        "review.json",
        "sources.json",
    ):
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
    tmp_path, payloads
) -> tuple[PipelineProviders, FakeLLM, FakeSERP, FakeExtractor, FakeImage, Settings]:
    settings = _settings(tmp_path)
    llm = FakeLLM([json.dumps(p) for p in payloads], settings)
    serp = FakeSERP()
    extractor = FakeExtractor()
    image = FakeImage(settings)
    providers = PipelineProviders(
        llm=llm, serp=serp, extractor=extractor, image=image, cms=None
    )
    return providers, llm, serp, extractor, image, settings


async def _full_run(
    session, job_row, providers, settings, **kwargs
) -> GenerationJob:
    """Run the pipeline on the caller's session (P8 convention: the test
    keeps the session open so it can refresh the job row and count rows,
    and closes it at the end of its own ``with SessionLocal()`` block)."""
    return await run_job_pipeline(
        session, job_row, providers, settings=settings, **kwargs
    )


def _count(session, model, job_id) -> int:
    return session.scalar(
        select(func.count()).select_from(model).where(model.job_id == job_id)
    )


# ---------------------------------------------------------------------------
# retry by step (spec 44/43.3)
# ---------------------------------------------------------------------------
async def test_retry_from_step_14_keeps_earlier_checkpoints(job, tmp_path):
    # Run 1: full chain with the LLM exhausted right after the writer draft —
    # the first image_plan call (payload 15) has no scripted answer, so the
    # step raises and the orchestrator persists ``failed``.
    providers, llm, _, _, image, settings = _providers(tmp_path, _payloads()[:14])
    with SessionLocal() as session:
        job_row = session.get(GenerationJob, job)
        with pytest.raises(PipelineError):
            await _full_run(session, job_row, providers, settings)
        session.refresh(job_row)
        assert job_row.status == JobStatus.FAILED.value
        assert job_row.error_code == "LLM_UNAVAILABLE"

        # Exactly 14 of the 15 scripted payloads were consumed (steps 1..13
        # all reached the LLM); step 14 (image_plan) hit an exhausted script.
        assert len(llm.calls) == 14
        # Steps 1..13 checkpointed before the failure: writer v1 + revision
        # v2, plus the three reviews.
        assert _count(session, ArticleVersionRow, job) == 2
        assert _count(session, ImageRow, job) == 0

    # Run 2: retry from step 14. reset_from_step(14) deletes only the image
    # rows (every earlier checkpoint survives), so the chain re-runs only the
    # image planner (step 14, 1 LLM call) + image generation (step 15, 0 LLM
    # calls). A single scripted image_plan payload is exactly enough.
    providers2, llm2, _, _, image2, settings2 = _providers(
        tmp_path, _payloads_from(14)
    )
    with SessionLocal() as session:
        job_row = session.get(GenerationJob, job)
        result = await _full_run(
            session, job_row, providers2, settings=settings2, retry_step=14
        )

    assert result.status == JobStatus.READY.value

    with SessionLocal() as session:
        # Only the tail of the chain ran: image_plan (1 LLM call) +
        # image_generation (no LLM). The writer draft and reviews from
        # run 1 were checkpointed and are reused, not regenerated.
        assert len(llm2.calls) == 1
        # Earlier checkpoints survived untouched.
        assert _count(session, SerpRun, job) == 1
        assert _count(session, JobSource, job) == TOP_N
        assert _count(session, CompetitorAnalysisRow, job) == TOP_N
        assert _count(session, SerpSynthesisRow, job) == 1
        assert _count(session, ContentBriefRow, job) == 1
        assert _count(session, ArticleOutlineRow, job) == 1
        # writer v1 + revision v2 from run 1 survive the retry untouched
        # (reset_from_step(14) deletes only the image rows, not versions).
        versions = session.scalars(
            select(ArticleVersionRow)
            .where(ArticleVersionRow.job_id == job)
            .order_by(ArticleVersionRow.version)
        ).all()
        assert [v.version for v in versions] == [1, 2]
        assert versions[0].stage == "writer"
        assert versions[1].stage == "revision"
        review_types = {
            r.review_type
            for r in session.scalars(
                select(ArticleReviewRow).where(ArticleReviewRow.job_id == job)
            ).all()
        }
        assert {"seo", "fact", "style"}.issubset(review_types)
        # The single image was re-planned and generated to a real file.
        images = session.scalars(
            select(ImageRow).where(ImageRow.job_id == job)
        ).all()
        assert len(images) == 1
        assert images[0].local_path
        assert Path(images[0].local_path).exists()


# ---------------------------------------------------------------------------
# resume from checkpoint (spec 9)
# ---------------------------------------------------------------------------
async def test_resume_from_first_incomplete_step(job, tmp_path):
    # Run 1: the LLM dies on the 3rd competitor-analysis call (payload 3 of
    # the 5 in step 4) → the job fails mid-research.
    providers, llm, _, _, _, settings = _providers(tmp_path, _payloads()[:3])
    with SessionLocal() as session:
        job_row = session.get(GenerationJob, job)
        with pytest.raises(PipelineError):
            await _full_run(session, job_row, providers, settings)
        session.refresh(job_row)
        assert job_row.status == JobStatus.FAILED.value

        # Checkpoints: steps 1-3 done, step 4 partial → first incomplete is 4.
        assert checkpoints.first_incomplete_step(session, job_row) == 4
        assert _count(session, SerpRun, job) == 1
        assert _count(session, JobSource, job) == TOP_N
        assert _count(session, CompetitorAnalysisRow, job) == 3
        assert _count(session, SerpSynthesisRow, job) == 0

    # Run 2: resume from step 4. Nothing is deleted; the three already-
    # analysed sources are skipped (per-source idempotency), so step 4 makes
    # 2 new analysis calls and steps 5..14 make one each — 12 payloads.
    providers2, llm2, _, _, _, settings2 = _providers(
        tmp_path, [ANALYSIS_TMPL] * 2 + _payloads()[5:]
    )
    with SessionLocal() as session:
        job_row = session.get(GenerationJob, job)
        result = await _full_run(
            session, job_row, providers2, settings=settings2,
            resume_from_step=4,
        )

    assert result.status == JobStatus.READY.value
    assert len(llm2.calls) == 12

    with SessionLocal() as session:
        # No second SERP run / source links: resume skipped steps 2-3.
        assert _count(session, SerpRun, job) == 1
        assert _count(session, JobSource, job) == TOP_N
        # Step 4 finished the two missing analyses (3 partial + 2 new).
        assert _count(session, CompetitorAnalysisRow, job) == TOP_N
        versions = session.scalars(
            select(ArticleVersionRow)
            .where(ArticleVersionRow.job_id == job)
            .order_by(ArticleVersionRow.version)
        ).all()
        assert [v.version for v in versions] == [1, 2]
        images = session.scalars(
            select(ImageRow).where(ImageRow.job_id == job)
        ).all()
        assert len(images) == 1
        assert images[0].local_path
        assert Path(images[0].local_path).exists()


# ---------------------------------------------------------------------------
# in-flight cancellation (spec 44/43.3)
# ---------------------------------------------------------------------------
async def test_cancel_mid_run_stops_at_next_step_boundary(job, tmp_path, monkeypatch):
    # Run 1: the LLM dies on the 3rd competitor-analysis call (payload 3 of
    # the 5 in step 4) → the job fails mid-research, exactly like the resume
    # test. Steps 1-3 are checkpointed, step 4 is partial (3 of 5 analyses).
    providers, llm, _, _, _, settings = _providers(tmp_path, _payloads()[:3])
    with SessionLocal() as session:
        job_row = session.get(GenerationJob, job)
        with pytest.raises(PipelineError):
            await _full_run(session, job_row, providers, settings)
        session.refresh(job_row)
        assert job_row.status == JobStatus.FAILED.value
        assert _count(session, CompetitorAnalysisRow, job) == 3

    # Run 2: resume from step 4 (the first incomplete step). The competitor
    # analysis runner is wrapped so that, *immediately after step 4 commits*,
    # a SEPARATE session flips the job to ``cancelled`` exactly the way the
    # web cancel route does (``api_cancel_job``). The worker keeps going into
    # step 5, where the per-boundary ``_cancelled`` check re-reads the DB and
    # stops the chain quietly — no further LLM calls, no failed/ready flip.
    import app.pipeline.orchestrator as orch
    from app.pipeline.steps.competitor_analysis import (
        run_competitor_analysis as real_competitor_analysis,
    )

    async def fake_competitor_analysis(session, job_row, llm):
        # Finish the real step (the 2 remaining sources), then cancel in a
        # separate session, mirroring a concurrent web click.
        await real_competitor_analysis(session, job_row, llm)
        with SessionLocal() as web:
            wjob = web.get(GenerationJob, job)
            wjob.status = JobStatus.CANCELLED.value
            wjob.current_step = "cancelled"
            wjob.completed_at = wjob.completed_at or datetime.now(timezone.utc)
            web.commit()

    monkeypatch.setattr(orch, "run_competitor_analysis", fake_competitor_analysis)

    # Only the 2 missing analyses can be reached before the boundary check
    # stops the chain (step 5 never runs). If the check regressed, step 5
    # would find an exhausted script and fail loudly — a strong assertion.
    providers2, llm2, _, _, _, settings2 = _providers(tmp_path, [ANALYSIS_TMPL] * 2)
    with SessionLocal() as session:
        job_row = session.get(GenerationJob, job)
        # The orchestrator returns quietly (no exception, no failed state).
        result = await _full_run(
            session, job_row, providers2, settings=settings2,
            resume_from_step=4,
        )
        assert result.status == JobStatus.CANCELLED.value

    with SessionLocal() as session:
        job_row = session.get(GenerationJob, job)
        # The cancellation stuck: never overridden to ready/failed. The
        # failure path (which would set current_step="failed" and re-raise)
        # was skipped by the boundary check; current_step is still the web
        # route's "cancelled". (error_code is left stale by design, exactly
        # like the production cancel route — it is not cleared on cancel.)
        assert job_row.status == JobStatus.CANCELLED.value
        assert job_row.current_step == "cancelled"
        assert job_row.completed_at is not None
        # Step 4 finished the two missing analyses (3 partial + 2 new = 5);
        # step 5 (synthesis) was skipped at its boundary → 0 synthesis rows.
        # Exactly 2 LLM calls were made in this run; none after the cancel.
        assert len(llm2.calls) == 2
        assert _count(session, CompetitorAnalysisRow, job) == TOP_N
        assert _count(session, SerpSynthesisRow, job) == 0
        assert _count(session, ContentBriefRow, job) == 0


# ---------------------------------------------------------------------------
# force source refresh (spec 14.2)
# ---------------------------------------------------------------------------
async def test_plain_rerun_cache_hit_vs_forced_rerun(job, tmp_path):
    # Run 1: a full cold run. The SourcePage TTL cache (spec 14.2) is empty,
    # so every top-5 organic URL misses and is extracted once → 5 extractor
    # calls, and the results are persisted as the reusable cache rows.
    providers, llm, _, extractor, _, settings = _providers(tmp_path, _payloads())
    with SessionLocal() as session:
        job_row = session.get(GenerationJob, job)
        result = await _full_run(session, job_row, providers, settings)
    assert result.status == JobStatus.READY.value
    # Cold cache: all 5 sources were extracted (one extract([url]) per URL),
    # PLUS one evidence_research (step 6) verification of the note's source.
    assert len(extractor.extract_calls) == TOP_N + 1
    # Count SourcePage rows directly (job-independent global cache table):
    # the 5 cache rows now back every later step-3 re-run.
    with SessionLocal() as session:
        n_pages = session.scalar(
            select(func.count())
            .select_from(SourcePage)
            .where(SourcePage.url.in_(ORGANIC_URLS))
        )
    assert n_pages == TOP_N

    # Run 2: a *plain* re-run of step 3 (``retry_step=3``). reset_from_step(3)
    # deletes the JobSource links and every later row, but NOT the SerpRun or
    # the SourcePage cache. Step 3 therefore finds every URL already fresh in
    # the TTL cache and makes ZERO extractor calls (spec 14.2: "默认不重复抓取").
    # Steps 4..14 re-run on the full LLM chain (5 analyses + 10 tail calls).
    providers2, llm2, _, extractor2, _, settings2 = _providers(tmp_path, _payloads())
    with SessionLocal() as session:
        job_row = session.get(GenerationJob, job)
        result2 = await _full_run(
            session, job_row, providers2, settings=settings2, retry_step=3
        )
    assert result2.status == JobStatus.READY.value
    # The whole point of the cache: a plain re-run re-fetches no *source* page
    # (step 3 hits the TTL cache). The only extract call is the H09
    # evidence_research (step 6) verification of the note's source URL.
    assert extractor2.extract_calls == [[EVIDENCE_SOURCE_URL]]
    assert len(llm2.calls) == 15

    # Run 3: the same re-run but with ``force_source_refresh=True``. The TTL
    # read is bypassed, so every URL is re-extracted → 5 extractor calls
    # (the upsert below each miss refreshes the same cache rows).
    providers3, llm3, _, extractor3, _, settings3 = _providers(tmp_path, _payloads())
    with SessionLocal() as session:
        job_row = session.get(GenerationJob, job)
        result3 = await _full_run(
            session,
            job_row,
            providers3,
            settings=settings3,
            retry_step=3,
            force_source_refresh=True,
        )
    assert result3.status == JobStatus.READY.value
    # Forced refresh re-extracts every top-5 source URL, plus the H09
    # evidence_research (step 6) verification of the note's source URL.
    called_urls = {url for batch in extractor3.extract_calls for url in batch}
    assert len(extractor3.extract_calls) == TOP_N + 1
    assert called_urls == set(ORGANIC_URLS[:TOP_N]) | {EVIDENCE_SOURCE_URL}
    assert len(llm3.calls) == 15


# ---------------------------------------------------------------------------
# cost tracking (spec 54)
# ---------------------------------------------------------------------------
async def test_cost_tracking_records_usage_and_provider_costs(job, tmp_path):
    """A full cold run records one ``llm_usage`` row per logical LLM call and
    persists every paid provider's reported cost on its own table."""
    providers, llm, _, extractor, image, settings = _providers(tmp_path, _payloads())
    with SessionLocal() as session:
        job_row = session.get(GenerationJob, job)
        result = await _full_run(session, job_row, providers, settings)
    assert result.status == JobStatus.READY.value
    # The full chain makes 15 successful logical LLM calls (step 4 ×5 plus
    # steps 5..14 ×1 each); the meter records one row per call.
    assert len(llm.calls) == 15

    with SessionLocal() as session:
        usage_rows = session.scalars(
            select(LLMUsageRow).where(LLMUsageRow.job_id == job)
        ).all()
        assert len(usage_rows) == 15
        step_counts: dict[str, int] = {}
        for r in usage_rows:
            step_counts[r.step] = step_counts.get(r.step, 0) + 1
        # 11 distinct LLM steps; only competitor_analysis makes >1 call.
        assert len(step_counts) == 11
        assert step_counts["competitor_analysis"] == 5
        for step, n in step_counts.items():
            if step != "competitor_analysis":
                assert n == 1, (step, n)
        # The scripted usage block is 1 prompt / 1 completion token per HTTP
        # call, and every logical call is exactly one HTTP call (no repairs).
        for r in usage_rows:
            assert r.input_tokens == 1
            assert r.output_tokens == 1
            assert r.duration_ms >= 0
            assert r.model == "test-model"

        # SERP run carries the credits DataForSEO reported (the fake returns
        # a flat 1.5 per query).
        run_row = session.scalars(select(SerpRun).where(SerpRun.job_id == job)).one()
        assert float(run_row.provider_cost) == pytest.approx(1.5)

        # Every source page records the Exa cost of its fresh extraction
        # (the fake returns 0.05 per page).
        pages = session.scalars(
            select(SourcePage).where(SourcePage.url.in_(ORGANIC_URLS[:TOP_N]))
        ).all()
        assert len(pages) == TOP_N
        for p in pages:
            assert float(p.provider_cost) == pytest.approx(0.05)

        # The one planned/generated image carries the image-provider cost.
        img = session.scalars(select(ImageRow).where(ImageRow.job_id == job)).one()
        assert float(img.provider_cost) == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# H04 regression: a DATASET (known) keyword must run the full chain to ready.
#
# Before the fix, prepare_keyword was the only SYNC step. The orchestrator
# awaits every step runner and treats a non-None, non-awaitable return as a
# bug -> TypeError -> the job failed with error_code UNEXPECTED. That only
# happened for a KNOWN keyword (the metrics value); unknown keywords returned
# None and slipped through, so the SERP-only path looked fine in tests.
# ---------------------------------------------------------------------------
async def test_known_keyword_full_run_reaches_ready(job, tmp_path):
    """A keyword present in the imported dataset completes to ``ready``.

    This is the exact H04 path: ``prepare_keyword`` returns a
    :class:`KeywordMetrics` (not None). With the sync step that value tripped
    the orchestrator's await; with the async step it resolves cleanly.
    """
    from decimal import Decimal

    cluster = KeywordCluster(name="h04test cluster", sheet_name="h04test")
    with SessionLocal() as session:
        session.add(cluster)
        session.commit()
        session.refresh(cluster)
        cluster_id = cluster.id

    try:
        with SessionLocal() as session:
            session.add(
                Keyword(
                    cluster_id=cluster_id,
                    keyword=KEYWORD,
                    volume=4200,
                    kd=Decimal("21.00"),
                    cpc=Decimal("1.5000"),
                    intent="Informational",
                    source="h04test",
                )
            )
            session.commit()

        providers, llm, _, _, _, settings = _providers(tmp_path, _payloads())
        with SessionLocal() as session:
            job_row = session.get(GenerationJob, job)
            result = await _full_run(session, job_row, providers, settings)

        # The job must reach ready — NOT fail with UNEXPECTED (the old
        # TypeError from awaiting the sync step's KeywordMetrics return).
        assert result.status == JobStatus.READY.value
        assert result.error_code is None

        with SessionLocal() as session:
            fresh = session.get(GenerationJob, job)
            assert fresh.keyword_metrics_available is True
            assert fresh.status == JobStatus.READY.value

        # L01: the section-34 directory is a COMPLETE offline export —
        # the research JSONs (brief/outline/serp/review/sources) are on
        # disk next to article.md/json, not only in the DB.
        import json as _json

        job_dir = Path(tmp_path) / "articles" / str(job)
        for name in (
            "article.md",
            "article.json",
            "content-brief.json",
            "outline.json",
            "serp.json",
            "review.json",
            "sources.json",
        ):
            art = job_dir / name
            assert art.exists(), f"missing section-34 artifact {name}"
            assert art.stat().st_size > 0, f"empty artifact {name}"

        # Spot-check the exported research payloads are real, not empty.
        serp = _json.loads((job_dir / "serp.json").read_text())
        assert serp["runs"], "serp.json should have at least one run"
        assert serp["runs"][0]["results"], "serp.json run should have results"
        review = _json.loads((job_dir / "review.json").read_text())
        assert review["reviews"], "review.json should have reviewer verdicts"
        src = _json.loads((job_dir / "sources.json").read_text())
        assert src["sources"], "sources.json should list job sources"
        brief = _json.loads((job_dir / "content-brief.json").read_text())
        assert brief, "content-brief.json should not be empty"
    finally:
        with SessionLocal() as session:
            session.execute(
                sa_delete(Keyword).where(Keyword.cluster_id == cluster_id)
            )
            session.execute(
                sa_delete(KeywordCluster).where(KeywordCluster.id == cluster_id)
            )
            session.commit()
