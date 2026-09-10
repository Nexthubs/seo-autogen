"""P5 unit: article writer, reviewers, anti-copy step, reviser
(SEO-AUTO-DEV-SPEC.md sections 24-29).

Scripted fake LLM + SQLite in-memory — no network, no external LLM.
"""

import json
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import JobStatus
from app.core.exceptions import ErrorCode, PipelineError
from app.db.base import Base
from app.db.models import GenerationJob
from app.db.models.article import ArticleReviewRow, ArticleVersionRow
from app.db.models.research import (
    ArticleOutlineRow,
    ContentBriefRow,
    EvidenceNoteRow,
    SerpSynthesisRow,
)
from app.db.models.source import JobSource, SourcePage
from app.pipeline.steps.anti_copy_step import run_anti_copy_check
from app.pipeline.steps.article_reviser import run_article_reviser
from app.pipeline.steps.article_writer import run_article_writer
from app.pipeline.steps.reviewers import (
    run_fact_review,
    run_seo_review,
    run_style_review,
)
from app.providers.llm.openai_compatible import OpenAICompatibleLLMProvider
from app.services.article_renderer import normalize_slug

FAST_BACKOFF = (0.001, 0.001, 0.001)
KEYWORD = "anxious attachment no contact"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

# A distinctive copied sentence (>= 12 words) that the writer will emit.
COPIED_SENTENCE = (
    "Anxious attachment is a pattern where people seek constant "
    "reassurance and fear abandonment in relationships."
)


def _settings() -> Settings:
    return Settings(
        llm_base_url="http://llm.test/v1",
        llm_api_key="test-key",
        llm_model="test-model",
        llm_max_retries=2,
        _env_file=None,
    )


def _chat_ok(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "cmpl-1",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )


class FakeLLM:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls: list[dict] = []
        self._settings = _settings()
        self._provider = OpenAICompatibleLLMProvider(
            settings=_settings(),
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
        return _chat_ok(self.payloads.pop(0))

    async def generate_structured(self, **kwargs):
        return await self._provider.generate_structured(**kwargs)

    async def aclose(self):
        await self._provider.aclose()


BRIEF = {
    "primary_keyword": KEYWORD,
    "secondary_keywords": ["avoidant attachment"],
    "long_tail_keywords": ["how long no contact"],
    "search_intent": "informational",
    "search_stage": "consideration",
    "article_strategy": "definitive guide",
    "target_reader": "adults in recovery",
    "core_problem": "confusion",
    "emotional_context": "anxious",
    "unique_angle": "evidence based",
    "content_gaps": ["science"],
    "required_topics": ["no contact", "signs"],
    "faq_questions": ["how long?"],
    "target_function": "coach",
    "cta_strategy": ["mid", "end"],
    "internal_link_markers": ["[coach]"],
    "recommended_word_count": 2500,
}

OUTLINE = {
    "title": f"{KEYWORD.title()}: A Complete Practical Guide",
    "sections": [
        {"heading": "What is avoidant attachment", "level": 2,
         "purpose": "x", "keywords": ["avoidant attachment"], "cta_slot": False},
        {"heading": "Practical steps", "level": 2,
         "purpose": "x", "keywords": ["no contact"], "cta_slot": True},
    ],
    "faq_questions": ["q1?"],
}

SYNTHESIS = {"dominant_intent": "informational", "common_topics": ["no contact"]}

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

FACT_REVIEW = {
    "issues": [
        {
            "quote_or_claim": "studies show no contact always works",
            "verdict": "soften",
            "reason": "not in the evidence notes",
        }
    ]
}

STYLE_REVIEW = {
    "score": 68,
    "ai_patterns": ["In today's fast-paced world"],
    "repetitive_patterns": ["it is important to"],
    "weak_sections": ["intro"],
    "required_changes": ["remove filler phrases"],
}

# The LLM's raw draft: contains an H1 (must be stripped) and a slug
# with spaces (must be normalized).
WRITER_DRAFT = {
    "title": "WRONG TITLE FROM LLM",
    "body_markdown": (
        "# Wrong H1\n\n## What is avoidant attachment\n\n"
        + COPIED_SENTENCE
        + "\n\n## Practical steps\n\n"
        "Take it one day at a time and keep a journal of your emotions.\n"
    ),
    "seo_title": "Anxious Attachment No Contact Guide",
    "meta_description": "A practical guide to anxious attachment no contact.",
    "slug": " Anxious Attachment  No-Contact Guide!! ",
}

REVISER_DRAFT = {
    "title": "should be ignored",
    "body_markdown": (
        "## What is avoidant attachment\n\n"
        "Attachment styles shape how adults pursue closeness in their "
        "partner relationships, and anxiety shows up as constant checking.\n"
        "## Practical steps\n\n"
        "Set a realistic boundary and write down your feelings each evening.\n"
    ),
    "seo_title": "Anxious Attachment No Contact (Revised)",
    "meta_description": "Revised practical guide.",
    "slug": "revised-slug",
}


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _seed(session)
        yield session
    engine.dispose()


@pytest.fixture()
def job(db):
    return db.scalars(select(GenerationJob)).one()


def _seed(session: Session) -> GenerationJob:
    job = GenerationJob(
        keyword=KEYWORD,
        status=JobStatus.OUTLINE_GENERATING.value,
        target_function="coach",
    )
    session.add(job)
    session.flush()

    # Five competitor sources. Source 1 contains the copied sentence so
    # the anti-copy check can flag the writer's draft (section 29).
    for i in range(1, 6):
        content = f"# Competitor {i}\n\n" + (
            ("Intro here. " + COPIED_SENTENCE + " Outro here. ")
            if i == 1
            else f"Unrelated body text for competitor number {i}. "
        )
        content += f"Filler {i}. " * 15
        page = SourcePage(
            url=f"https://site{i}.example.com/a{i}",
            normalized_url=f"https://site{i}.example.com/a{i}",
            url_hash=f"p5uh{i:036d}",
            title=f"Competitor {i}",
            domain=f"site{i}.example.com",
            content_markdown=content,
            content_hash=f"p5ch{i:036d}",
            extractor="test",
            first_seen_at=NOW,
            last_fetched_at=NOW,
        )
        session.add(page)
        session.flush()
        session.add(JobSource(job_id=job.id, source_page_id=page.id, serp_rank=i))

    session.add(ContentBriefRow(job_id=job.id, brief=BRIEF))
    session.add(
        ArticleOutlineRow(
            job_id=job.id,
            outline=OUTLINE,
            valid=True,
            repair_count=0,
        )
    )
    session.add(SerpSynthesisRow(job_id=job.id, synthesis=SYNTHESIS))
    session.add(
        EvidenceNoteRow(
            job_id=job.id,
            claim="Attachment styles affect adult relationships.",
            source_title="Hazan & Shaver 1987",
            source_url="https://doi.org/10.1037/0022-3514.53.3.519",
            source_type="peer-reviewed study",
            confidence="high",
            usage="supported",
        )
    )
    session.commit()
    return job


async def _run_writer(db, job) -> FakeLLM:
    llm = FakeLLM([json.dumps(WRITER_DRAFT)])
    await run_article_writer(db, job, llm, guideline_excerpt="GUIDELINE")
    return llm


# ============================================================
# writer (sections 24, 25, 28)
# ============================================================
async def test_writer_persists_version_1(db, job):
    llm = await _run_writer(db, job)
    await llm.aclose()

    rows = db.scalars(
        select(ArticleVersionRow).where(ArticleVersionRow.job_id == job.id)
    ).all()
    assert len(rows) == 1
    v1 = rows[0]
    assert v1.version == 1
    assert v1.stage == "writer"
    # title comes from the validated outline (section 6.1), not the LLM
    assert v1.title == OUTLINE["title"]
    # H1 stripped (section 5)
    assert "## Wrong H1" in v1.body_markdown
    assert "\n# " not in ("\n" + v1.body_markdown)
    # slug normalized program-side (section 6.2)
    assert v1.slug == normalize_slug(WRITER_DRAFT["slug"], OUTLINE["title"])
    assert v1.model == "test-model"
    assert v1.prompt_name == "article_writer"
    assert v1.prompt_version == "1.0"

    # status chain: writer advances to seo_reviewing
    db.refresh(job)
    assert job.status == JobStatus.SEO_REVIEWING.value
    assert job.current_step == "seo_reviewing"

    # section 25: NO competitor text in the writer prompt
    user = llm.calls[0]["user"]
    for i in range(1, 6):
        assert f"Competitor {i}" not in user
    assert "GUIDELINE" in user
    assert OUTLINE["title"] in user


async def test_writer_requires_brief_and_outline(db, job):
    db.delete(db.scalars(select(ContentBriefRow)).first())
    llm = FakeLLM([])
    with pytest.raises(PipelineError) as ei:
        await run_article_writer(db, job, llm, guideline_excerpt="G")
    assert ei.value.error_code is ErrorCode.ARTICLE_VALIDATION_FAILED
    await llm.aclose()

    db.add(ContentBriefRow(job_id=job.id, brief=BRIEF))
    db.flush()
    db.delete(db.scalars(select(ArticleOutlineRow)).first())
    db.flush()
    llm = FakeLLM([])
    with pytest.raises(PipelineError) as ei:
        await run_article_writer(db, job, llm, guideline_excerpt="G")
    assert ei.value.error_code is ErrorCode.ARTICLE_VALIDATION_FAILED
    await llm.aclose()


# ============================================================
# reviewers (sections 26, 50)
# ============================================================
async def test_three_reviews_persisted_and_status_chain(db, job):
    await _run_writer(db, job)

    llm = FakeLLM([json.dumps(SEO_REVIEW)])
    seo = await run_seo_review(db, job, llm, guideline_excerpt="GUIDELINE")
    await llm.aclose()
    db.refresh(job)
    assert job.status == JobStatus.FACT_REVIEWING.value

    llm = FakeLLM([json.dumps(FACT_REVIEW)])
    fact = await run_fact_review(db, job, llm, guideline_excerpt="GUIDELINE")
    await llm.aclose()
    db.refresh(job)
    assert job.status == JobStatus.STYLE_REVIEWING.value

    llm = FakeLLM([json.dumps(STYLE_REVIEW)])
    style = await run_style_review(db, job, llm, guideline_excerpt="GUIDELINE")
    await llm.aclose()
    db.refresh(job)
    assert job.status == JobStatus.ARTICLE_REVISING.value

    assert seo.total_score == 82
    assert fact.issues[0].verdict == "soften"
    assert style.score == 68

    reviews = db.scalars(
        select(ArticleReviewRow).where(ArticleReviewRow.job_id == job.id)
    ).all()
    assert {r.review_type for r in reviews} == {"seo", "fact", "style"}
    v1 = db.scalars(select(ArticleVersionRow)).first()
    assert all(r.article_version_id == v1.id for r in reviews)
    assert all(r.model == "test-model" for r in reviews)
    assert all(r.prompt_version == "1.0" for r in reviews)
    # fact review carries its full issue payload
    fact_row = next(r for r in reviews if r.review_type == "fact")
    assert fact_row.review["issues"][0]["verdict"] == "soften"


async def test_reviewers_reject_without_article(db, job):
    llm = FakeLLM([])
    with pytest.raises(PipelineError) as ei:
        await run_seo_review(db, job, llm, guideline_excerpt="G")
    assert ei.value.error_code is ErrorCode.ARTICLE_VALIDATION_FAILED
    await llm.aclose()


async def test_reviewer_rerun_replaces_same_version_type(db, job):
    await _run_writer(db, job)
    for _ in range(2):
        llm = FakeLLM([json.dumps(SEO_REVIEW)])
        await run_seo_review(db, job, llm, guideline_excerpt="G")
        await llm.aclose()
    count = db.query(ArticleReviewRow).count()
    assert count == 1
    # status walked back to fact_reviewing on each run
    db.refresh(job)
    assert job.status == JobStatus.FACT_REVIEWING.value


async def test_fact_review_prompt_carries_evidence(db, job):
    await _run_writer(db, job)
    llm = FakeLLM([json.dumps(FACT_REVIEW)])
    await run_fact_review(db, job, llm, guideline_excerpt="G")
    await llm.aclose()
    user = llm.calls[0]["user"]
    assert "Hazan & Shaver 1987" in user
    assert "Attachment styles affect adult relationships." in user
    # no competitor text (section 50)
    assert "Competitor" not in user


# ============================================================
# anti-copy step (section 29)
# ============================================================
async def test_anti_copy_flags_written_draft(db, job):
    await _run_writer(db, job)
    v1 = db.scalars(select(ArticleVersionRow)).first()

    report = run_anti_copy_check(db, job, v1)

    assert report.has_serious_overlap
    assert len(report.matches) == 1
    match = report.matches[0]
    assert match.source_url == "https://site1.example.com/a1"
    assert match.word_count >= 12
    assert match.similarity >= 0.85

    # persisted as an "anticopy" review on the version
    row = db.scalars(
        select(ArticleReviewRow).where(
            ArticleReviewRow.review_type == "anticopy"
        )
    ).first()
    assert row is not None
    assert row.article_version_id == v1.id
    assert row.review["has_serious_overlap"] is True


# ============================================================
# reviser (sections 27, 28)
# ============================================================
async def test_reviser_appends_version_2_and_advances(db, job):
    await _run_writer(db, job)
    llm = FakeLLM([json.dumps(SEO_REVIEW)])
    await run_seo_review(db, job, llm, guideline_excerpt="G")
    await llm.aclose()
    llm = FakeLLM([json.dumps(FACT_REVIEW)])
    await run_fact_review(db, job, llm, guideline_excerpt="G")
    await llm.aclose()
    llm = FakeLLM([json.dumps(STYLE_REVIEW)])
    await run_style_review(db, job, llm, guideline_excerpt="G")
    await llm.aclose()

    llm = FakeLLM([json.dumps(REVISER_DRAFT)])
    revised, final_anticopy = await run_article_reviser(db, job, llm)
    await llm.aclose()

    # versioning: v1 (writer) and v2 (revision) coexist — never overwrite
    rows = db.scalars(
        select(ArticleVersionRow)
        .where(ArticleVersionRow.job_id == job.id)
        .order_by(ArticleVersionRow.version)
    ).all()
    assert [r.version for r in rows] == [1, 2]
    assert [r.stage for r in rows] == ["writer", "revision"]
    assert rows[0].body_markdown != rows[1].body_markdown  # v1 untouched
    assert rows[1].title == OUTLINE["title"]
    assert rows[1].model == "test-model"
    assert rows[1].prompt_name == "article_reviser"
    assert rows[1].prompt_version == "1.0"

    # revised doc: H1-stripped, slug normalized
    assert isinstance(revised.body_markdown, str)
    assert "\n# " not in ("\n" + rows[1].body_markdown)
    assert rows[1].slug == "revised-slug"

    # anti-copy: flagged on the draft, clean on the revision
    assert final_anticopy.matches == []
    draft_anticopy = db.scalars(
        select(ArticleReviewRow).where(
            ArticleReviewRow.review_type == "anticopy",
            ArticleReviewRow.article_version_id == rows[0].id,
        )
    ).first()
    assert draft_anticopy is not None
    assert draft_anticopy.review["has_serious_overlap"] is True
    revised_anticopy = db.scalars(
        select(ArticleReviewRow).where(
            ArticleReviewRow.review_type == "anticopy",
            ArticleReviewRow.article_version_id == rows[1].id,
        )
    ).first()
    assert revised_anticopy is not None
    assert revised_anticopy.review["has_serious_overlap"] is False

    # status: P5 ends at IMAGE_PLANNING (the first P6 status)
    db.refresh(job)
    assert job.status == JobStatus.IMAGE_PLANNING.value

    # section 27: the reviser prompt carries draft + all 3 reviews +
    # the anti-copy flags
    user = llm.calls[0]["user"]
    assert "Original draft:" in user
    assert COPIED_SENTENCE in user
    assert "shorten the meta description" in user
    assert "soften" in user
    assert "In today's fast-paced world" in user
    assert "possible_source_overlap" in user
    assert "site1.example.com" in user
    # and NO raw competitor text beyond the flagged phrases
    assert "Filler 1." not in user


async def test_reviser_requires_draft(db, job):
    llm = FakeLLM([])
    with pytest.raises(PipelineError) as ei:
        await run_article_reviser(db, job, llm)
    assert ei.value.error_code is ErrorCode.ARTICLE_VALIDATION_FAILED
    await llm.aclose()
