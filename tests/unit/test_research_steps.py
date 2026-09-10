"""P4 unit: research pipeline steps (spec sections 9, 16-18, 22, 23, 50).

Every step is exercised with a scripted fake LLM (httpx.MockTransport)
and a SQLite in-memory database — no network, no external LLM.
"""

import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import JobStatus
from app.core.exceptions import ErrorCode, PipelineError
from app.db.base import Base
from app.db.models import (
    GenerationJob,
    InternalLinkRule,
    Keyword,
    KeywordCluster,
)
from app.db.models.research import (
    ArticleOutlineRow,
    CompetitorAnalysisRow,
    ContentBriefRow,
    EvidenceNoteRow,
    SerpSynthesisRow,
)
from app.db.models.serp import SerpResult, SerpRun
from app.db.models.source import JobSource, SourcePage
from app.pipeline.steps.competitor_analysis import run_competitor_analysis
from app.pipeline.steps.content_brief import run_content_brief
from app.pipeline.steps.evidence_research import run_evidence_research
from app.pipeline.steps.outline import run_outline
from app.pipeline.steps.serp_synthesis import run_serp_synthesis
from app.providers.extractor.base import ContentExtractor
from app.providers.llm.openai_compatible import OpenAICompatibleLLMProvider
from app.schemas.research import (
    ArticleOutline,
    CompetitorAnalysis,
    ContentBrief,
    SERPSynthesis,
)
from app.schemas.sources import ExtractedPage
from app.services.outline_validator import validate_outline
from app.services.prompt_service import load_prompt

FAST_BACKOFF = (0.001, 0.001, 0.001)


# ============================================================
# fake LLM
# ============================================================
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
    """Scripted LLM: pops the next scripted payload per call."""

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
        user_prompt = body["messages"][1]["content"]
        self.calls.append(
            {"system": body["messages"][0]["content"], "user": user_prompt}
        )
        return _chat_ok(self.payloads.pop(0))

    async def generate_structured(self, **kwargs):
        return await self._provider.generate_structured(**kwargs)

    async def generate_text(self, **kwargs):
        return await self._provider.generate_text(**kwargs)

    async def aclose(self):
        await self._provider.aclose()


# ============================================================
# fixture data
# ============================================================
KEYWORD = "anxious attachment no contact"

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

ANALYSIS_TMPL = {
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
    },
    {
        "claim": "30-day no contact improves outcomes.",
        "source_title": "No study found",
        "source_url": "https://example.com/blog",
        "source_type": "blog",
        "confidence": "low",
        "usage": "avoid",
        "note": None,
    },
]

BRIEF = {
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
    "internal_link_markers": ["[coach]"],
    "recommended_word_count": 2500,
}


def _valid_outline() -> dict:
    def sec(heading, level=2, keywords=(), cta=False):
        return {
            "heading": heading,
            "level": level,
            "purpose": "x",
            "keywords": list(keywords),
            "cta_slot": cta,
        }

    return {
        "title": f"{KEYWORD.title()}: A Complete Practical Guide",
        "sections": [
            sec("What is avoidant attachment", 2, ["avoidant attachment"]),
            sec("No contact explained", 2, ["no contact"]),
            sec("Signs of an avoidant partner", 2, ["signs"]),
            sec("Practical steps", 2, ["steps"]),
            sec("Common exercises", 2, ["exercises"], cta=True),
            sec("FAQ", 2, ["faq"]),
        ],
        "faq_questions": ["q1?", "q2?", "q3?"],
    }


def _invalid_outline() -> dict:
    o = _valid_outline()
    o["title"] = "No keyword in this title"  # fails the title rule
    return o


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
        status=JobStatus.SOURCE_EXTRACTING.value,
        target_function="coach",
    )
    session.add(job)
    session.flush()

    # 5 competitor sources (rank order)
    for i in range(1, 6):
        page = SourcePage(
            url=f"https://site{i}.example.com/a{i}",
            normalized_url=f"https://site{i}.example.com/a{i}",
            url_hash=f"uh{i:040d}",
            title=f"Competitor {i}",
            domain=f"site{i}.example.com",
            content_markdown=f"# Competitor {i}\n\nBody text {i}. " * 20,
            content_hash=f"ch{i:040d}",
            extractor="test",
            first_seen_at=NOW,
            last_fetched_at=NOW,
        )
        session.add(page)
        session.flush()
        session.add(JobSource(job_id=job.id, source_page_id=page.id, serp_rank=i))

    # SERP run with PAA + related
    run = SerpRun(
        job_id=job.id,
        provider="fake",
        query=KEYWORD,
        location_code=2840,
        language_code="US_EN",
        device="Desktop",
        raw_response={"ok": True},
    )
    session.add(run)
    session.flush()
    session.add_all(
        [
            SerpResult(
                serp_run_id=run.id,
                result_type="paa",
                title="Why does no contact work?",
                raw_item={},
            ),
            SerpResult(
                serp_run_id=run.id,
                result_type="related",
                title="no contact rules",
                raw_item={},
            ),
        ]
    )

    # dataset + one internal link rule
    cluster = KeywordCluster(name="Attachment", sheet_name="Attachment")
    session.add(cluster)
    session.flush()
    session.add(
        Keyword(
            cluster_id=cluster.id,
            keyword=KEYWORD,
            volume=720,
            kd=28,
            cpc=0.8,
            intent="Informational",
            source="excel",
        )
    )
    session.add(
        InternalLinkRule(
            marker="[coach]", anchor_text="book a call", target_url="/coach",
            keywords=["coach"], active=True,
        )
    )
    session.commit()
    return job


def _analysis_payload(index: int) -> str:
    d = dict(ANALYSIS_TMPL)
    return json.dumps(d)


# ============================================================
# competitor analysis
# ============================================================
async def test_competitor_analysis_happy_path(db, job):
    llm = FakeLLM([_analysis_payload(i) for i in range(5)])
    try:
        analyses = await run_competitor_analysis(db, job, llm)
    finally:
        await llm.aclose()

    assert len(analyses) == 5
    assert all(isinstance(a, CompetitorAnalysis) for a in analyses)

    rows = db.scalars(
        select(CompetitorAnalysisRow).where(CompetitorAnalysisRow.job_id == job.id)
    ).all()
    assert len(rows) == 5
    assert {r.analysis["source_id"] for r in rows} == {
        str(a.source_id) for a in analyses
    }
    assert rows[0].model == "test-model"
    assert rows[0].prompt_version == "1.0"

    # context check (section 50): each call carries ONE competitor text
    for i, call in enumerate(llm.calls):
        assert f"Competitor {i + 1}" in call["user"]
        for j in range(1, 6):
            if j != i + 1:
                assert f"# Competitor {j}" not in call["user"]
        assert KEYWORD in call["user"]

    # status: analyzing, then stays until synthesis advances it
    db.refresh(job)
    assert job.status == JobStatus.SERP_ANALYZING.value
    assert job.current_step == "competitor_analyzing"


async def test_competitor_analysis_forces_real_source_id(db, job):
    """The echoed source_id is replaced by the actual page id."""
    job = db.get(GenerationJob, db.scalars(select(GenerationJob.id)).first())
    pages = db.scalars(
        select(SourcePage).join(
            JobSource, JobSource.source_page_id == SourcePage.id
        ).where(JobSource.job_id == job.id).order_by(JobSource.serp_rank)
    ).all()
    payload = json.dumps(ANALYSIS_TMPL)  # zero UUID
    llm = FakeLLM([payload] * 5)
    try:
        analyses = await run_competitor_analysis(db, job, llm)
    finally:
        await llm.aclose()
    assert analyses[0].source_id == pages[0].id


async def test_competitor_analysis_requires_sources(db, job):
    for js in db.scalars(
        select(JobSource).where(JobSource.job_id == job.id)
    ).all():
        db.delete(js)
    db.commit()
    llm = FakeLLM([])
    with pytest.raises(PipelineError) as ei:
        await run_competitor_analysis(db, job, llm)
    assert ei.value.error_code is ErrorCode.SOURCE_EMPTY
    await llm.aclose()


# ============================================================
# serp synthesis
# ============================================================
async def test_serp_synthesis_happy_path(db, job):
    await run_competitor_analysis(
        db, job, FakeLLM([_analysis_payload(i) for i in range(5)])
    )
    llm = FakeLLM([json.dumps(SYNTHESIS)])
    try:
        syn = await run_serp_synthesis(db, job, llm)
    finally:
        await llm.aclose()

    assert isinstance(syn, SERPSynthesis)
    row = db.scalars(select(SerpSynthesisRow)).first()
    assert row.synthesis["dominant_intent"] == "informational"
    assert row.model == "test-model"

    call = llm.calls[0]["user"]
    # structured analyses + PAA + related in the prompt (section 50)
    assert "potential_gaps" in call
    assert "Why does no contact work?" in call
    assert "no contact rules" in call
    # full texts must NOT be in this prompt (section 17 / 50)
    assert "# Competitor" not in call

    db.refresh(job)
    assert job.status == JobStatus.EVIDENCE_RESEARCHING.value


async def test_serp_synthesis_replaces_on_rerun(db, job):
    await run_competitor_analysis(
        db, job, FakeLLM([_analysis_payload(i) for i in range(5)])
    )
    for payload in (json.dumps(SYNTHESIS), json.dumps(SYNTHESIS)):
        llm = FakeLLM([payload])
        try:
            await run_serp_synthesis(db, job, llm)
        finally:
            await llm.aclose()
    assert db.query(SerpSynthesisRow).count() == 1


# ============================================================
# evidence research
# ============================================================
async def test_evidence_research_persists_notes(db, job):
    await run_competitor_analysis(
        db, job, FakeLLM([_analysis_payload(i) for i in range(5)])
    )
    await run_serp_synthesis(db, job, FakeLLM([json.dumps(SYNTHESIS)]))

    llm = FakeLLM([json.dumps({"notes": EVIDENCE_NOTES})])
    try:
        notes = await run_evidence_research(db, job, llm)
    finally:
        await llm.aclose()

    assert len(notes) == 2
    rows = db.scalars(
        select(EvidenceNoteRow).where(EvidenceNoteRow.job_id == job.id)
    ).all()
    assert len(rows) == 2
    assert rows[0].confidence == "high"
    assert rows[0].usage == "supported"
    assert rows[1].note is None

    # context check (section 18): no competitor text, synthesis topic only
    call = llm.calls[0]["user"]
    assert "# Competitor" not in call
    assert "informational" in call

    db.refresh(job)
    assert job.status == JobStatus.BRIEF_GENERATING.value


async def test_evidence_research_replaces_on_rerun(db, job):
    await run_competitor_analysis(
        db, job, FakeLLM([_analysis_payload(i) for i in range(5)])
    )
    await run_serp_synthesis(db, job, FakeLLM([json.dumps(SYNTHESIS)]))
    for _ in range(2):
        llm = FakeLLM([json.dumps({"notes": EVIDENCE_NOTES[:1]})])
        try:
            await run_evidence_research(db, job, llm)
        finally:
            await llm.aclose()
    assert db.query(EvidenceNoteRow).count() == 1


async def test_evidence_research_empty_notes_fails(db, job):
    await run_competitor_analysis(
        db, job, FakeLLM([_analysis_payload(i) for i in range(5)])
    )
    await run_serp_synthesis(db, job, FakeLLM([json.dumps(SYNTHESIS)]))
    llm = FakeLLM([json.dumps({"notes": []})])
    with pytest.raises(PipelineError) as ei:
        await run_evidence_research(db, job, llm)
    assert ei.value.error_code is ErrorCode.LLM_STRUCTURED_OUTPUT_INVALID
    await llm.aclose()


# ============================================================
# evidence source verification (H09 — content guideline section 28)
#
# An LLM self-report is not proof a source exists. When an independent
# content-extractor channel is wired, each note's source URL is fetched; a
# source that cannot be fetched, or that returns empty content, is downgraded
# to ``confidence="low"`` / ``usage="avoid"`` before the note is persisted.
# ============================================================
class _FakeVerifier(ContentExtractor):
    """Scripted extractor: ``behaviour`` maps url -> page | exception | None."""

    def __init__(self, behaviour: dict):
        self.behaviour = behaviour

    async def extract(self, urls: list[str]) -> list[ExtractedPage]:
        pages: list[ExtractedPage] = []
        for url in urls:
            action = self.behaviour.get(url)
            if action is None:  # url not scripted at all → "unreachable"
                continue
            if isinstance(action, BaseException):
                raise action
            pages.append(
                ExtractedPage(
                    url=url,
                    normalized_url=url,
                    title="V",
                    content_markdown=action,
                    extracted_at=datetime.now(timezone.utc),
                    extractor="fake",
                )
            )
        return pages

    async def health_check(self) -> bool:
        return True


async def _prep_research(db, job):
    await run_competitor_analysis(
        db, job, FakeLLM([_analysis_payload(i) for i in range(5)])
    )
    await run_serp_synthesis(db, job, FakeLLM([json.dumps(SYNTHESIS)]))


async def test_evidence_research_no_verifier_keeps_notes(db, job):
    # No verifier (focused unit caller): notes persist untouched.
    await _prep_research(db, job)
    llm = FakeLLM([json.dumps({"notes": EVIDENCE_NOTES})])
    try:
        notes = await run_evidence_research(db, job, llm)
    finally:
        await llm.aclose()
    assert notes[0].confidence == "high"
    assert notes[0].usage == "supported"
    assert notes[1].confidence == "low"
    assert notes[1].usage == "avoid"


async def test_evidence_research_verified_source_kept(db, job):
    # Reachable source whose body actually corroborates the title + claim →
    # note kept as reported (R-H06: non-empty text alone is NOT enough).
    await _prep_research(db, job)
    url = EVIDENCE_NOTES[0]["source_url"]
    verifier = _FakeVerifier(
        {
            url: (
                "Hazan and Shaver (1987) found that attachment styles shape "
                "adult romantic relationships across the lifespan."
            )
        }
    )
    llm = FakeLLM([json.dumps({"notes": EVIDENCE_NOTES[:1]})])
    try:
        notes = await run_evidence_research(db, job, llm, verifier=verifier)
    finally:
        await llm.aclose()
    assert notes[0].confidence == "high"
    assert notes[0].usage == "supported"
    assert notes[0].note == "cite properly"  # unchanged
    assert notes[0].verification_status == "supported"
    assert "attachment styles" in (notes[0].supporting_excerpt or "")
    row = db.scalars(
        select(EvidenceNoteRow).where(EvidenceNoteRow.job_id == job.id)
    ).first()
    assert row.usage == "supported"
    assert row.confidence == "high"
    assert row.verification_status == "supported"
    assert row.supporting_excerpt


async def test_evidence_research_unreachable_source_downgraded(db, job):
    # Source not fetched at all → downgraded to low/avoid with a marker.
    await _prep_research(db, job)
    url = EVIDENCE_NOTES[0]["source_url"]
    verifier = _FakeVerifier({})  # url unknown → unreachable
    llm = FakeLLM([json.dumps({"notes": EVIDENCE_NOTES[:1]})])
    try:
        notes = await run_evidence_research(db, job, llm, verifier=verifier)
    finally:
        await llm.aclose()
    assert notes[0].confidence == "low"
    assert notes[0].usage == "avoid"
    assert "source_unverified" in (notes[0].note or "")
    row = db.scalars(
        select(EvidenceNoteRow).where(EvidenceNoteRow.job_id == job.id)
    ).first()
    assert row.usage == "avoid"
    assert row.confidence == "low"


async def test_evidence_research_empty_content_downgraded(db, job):
    # Source fetched but empty body → downgraded.
    await _prep_research(db, job)
    url = EVIDENCE_NOTES[0]["source_url"]
    verifier = _FakeVerifier({url: "   "})
    llm = FakeLLM([json.dumps({"notes": EVIDENCE_NOTES[:1]})])
    try:
        notes = await run_evidence_research(db, job, llm, verifier=verifier)
    finally:
        await llm.aclose()
    assert notes[0].usage == "avoid"
    assert notes[0].confidence == "low"
    assert "empty content" in (notes[0].note or "")


async def test_evidence_research_fetch_exception_downgraded(db, job):
    # Verifier raises (network/404) → treated as unreachable, not fatal.
    await _prep_research(db, job)
    url = EVIDENCE_NOTES[0]["source_url"]
    verifier = _FakeVerifier({url: httpx.ConnectError("boom")})
    llm = FakeLLM([json.dumps({"notes": EVIDENCE_NOTES[:1]})])
    try:
        notes = await run_evidence_research(db, job, llm, verifier=verifier)
    finally:
        await llm.aclose()
    assert notes[0].usage == "avoid"
    assert notes[0].confidence == "low"
    assert "fetch failed" in (notes[0].note or "")


# --- R-H06: reachable-but-unsupporting sources must be downgraded ----------
async def test_r_h06_reachable_but_irrelevant_source_softened(db, job):
    # A live page that does not corroborate the proposed paper/number must not
    # ship as high/supported (the audit's "Welcome to our homepage" case).
    await _prep_research(db, job)
    url = EVIDENCE_NOTES[0]["source_url"]
    verifier = _FakeVerifier({url: "Welcome to our homepage. Contact us."})
    llm = FakeLLM([json.dumps({"notes": EVIDENCE_NOTES[:1]})])
    try:
        notes = await run_evidence_research(db, job, llm, verifier=verifier)
    finally:
        await llm.aclose()
    assert notes[0].usage == "soften"
    assert notes[0].confidence == "low"
    assert notes[0].verification_status == "unsupported"
    assert notes[0].supporting_excerpt
    row = db.scalars(
        select(EvidenceNoteRow).where(EvidenceNoteRow.job_id == job.id)
    ).first()
    assert row.usage == "soften"
    assert row.verification_status == "unsupported"
    assert row.supporting_excerpt


async def test_r_h06_contradicting_source_avoided(db, job):
    await _prep_research(db, job)
    url = EVIDENCE_NOTES[0]["source_url"]
    verifier = _FakeVerifier(
        {
            url: (
                "There is no evidence that attachment styles affect adult "
                "relationships in the general population."
            )
        }
    )
    llm = FakeLLM([json.dumps({"notes": EVIDENCE_NOTES[:1]})])
    try:
        notes = await run_evidence_research(db, job, llm, verifier=verifier)
    finally:
        await llm.aclose()
    assert notes[0].usage == "avoid"
    assert notes[0].verification_status == "contradicted"
    assert "source_contradicted" in (notes[0].note or "")


# ============================================================
# content brief
# ============================================================
async def test_content_brief_happy_path(db, job):
    await run_competitor_analysis(
        db, job, FakeLLM([_analysis_payload(i) for i in range(5)])
    )
    await run_serp_synthesis(db, job, FakeLLM([json.dumps(SYNTHESIS)]))
    await run_evidence_research(
        db, job, FakeLLM([json.dumps({"notes": EVIDENCE_NOTES})])
    )

    llm = FakeLLM([json.dumps(BRIEF)])
    try:
        brief = await run_content_brief(db, job, llm)
    finally:
        await llm.aclose()

    assert isinstance(brief, ContentBrief)
    row = db.scalars(select(ContentBriefRow)).first()
    assert row.brief["primary_keyword"] == KEYWORD
    assert row.model == "test-model"

    call = llm.calls[0]["user"]
    # dataset metrics available (P3 lookup)
    assert "720" in call
    # evidence notes + allowed markers (section 22)
    assert "Hazan & Shaver 1987" in call
    assert "[coach]" in call
    assert "coach" in call  # target function

    db.refresh(job)
    assert job.status == JobStatus.OUTLINE_GENERATING.value


async def test_content_brief_prompt_carries_job_strategy(db, job):
    """M08: job.strategy previously never reached the brief prompt."""
    await run_competitor_analysis(
        db, job, FakeLLM([_analysis_payload(i) for i in range(5)])
    )
    await run_serp_synthesis(db, job, FakeLLM([json.dumps(SYNTHESIS)]))
    await run_evidence_research(
        db, job, FakeLLM([json.dumps({"notes": EVIDENCE_NOTES})])
    )

    job.strategy = "low_kd"
    db.flush()

    llm = FakeLLM([json.dumps(BRIEF)])
    try:
        await run_content_brief(db, job, llm)
    finally:
        await llm.aclose()

    user = llm.calls[0]["user"]
    assert "Content strategy: low_kd" in user
    # the meaning of the strategy must be explained to the model
    assert "lower-competition angle" in user


async def test_content_brief_strips_unallowed_markers(db, job):
    await run_competitor_analysis(
        db, job, FakeLLM([_analysis_payload(i) for i in range(5)])
    )
    await run_serp_synthesis(db, job, FakeLLM([json.dumps(SYNTHESIS)]))
    await run_evidence_research(
        db, job, FakeLLM([json.dumps({"notes": EVIDENCE_NOTES})])
    )

    bad_brief = dict(BRIEF)
    bad_brief["internal_link_markers"] = ["[coach]", "[bogus]"]
    llm = FakeLLM([json.dumps(bad_brief)])
    try:
        brief = await run_content_brief(db, job, llm)
    finally:
        await llm.aclose()

    assert brief.internal_link_markers == ["[coach]"]
    row = db.scalars(select(ContentBriefRow)).first()
    assert row.brief["internal_link_markers"] == ["[coach]"]


# ============================================================
# outline (generation + validation + repair)
# ============================================================
async def test_outline_happy_path_no_repairs(db, job):
    await run_competitor_analysis(
        db, job, FakeLLM([_analysis_payload(i) for i in range(5)])
    )
    await run_serp_synthesis(db, job, FakeLLM([json.dumps(SYNTHESIS)]))
    await run_evidence_research(
        db, job, FakeLLM([json.dumps({"notes": EVIDENCE_NOTES})])
    )
    await run_content_brief(db, job, FakeLLM([json.dumps(BRIEF)]))

    llm = FakeLLM([json.dumps(_valid_outline())])
    try:
        outline, repairs = await run_outline(
            db, job, llm, guideline_excerpt="GUIDELINE TEXT"
        )
    finally:
        await llm.aclose()

    assert validate_outline(outline) == []
    assert repairs == 0
    row = db.scalars(select(ArticleOutlineRow)).first()
    assert row.valid is True
    assert row.repair_count == 0
    assert row.outline["title"] == outline.title

    call = llm.calls[0]["user"]
    assert "GUIDELINE TEXT" in call
    assert "required_topics" in call

    db.refresh(job)
    assert job.status == JobStatus.ARTICLE_GENERATING.value
    assert job.current_step == "article_generating"


async def test_outline_repair_path_succeeds(db, job):
    """First outline invalid -> repair prompt with violations -> ok."""
    job = db.get(GenerationJob, db.scalars(select(GenerationJob.id)).first())
    await run_competitor_analysis(
        db, job, FakeLLM([_analysis_payload(i) for i in range(5)])
    )
    await run_serp_synthesis(db, job, FakeLLM([json.dumps(SYNTHESIS)]))
    await run_evidence_research(
        db, job, FakeLLM([json.dumps({"notes": EVIDENCE_NOTES})])
    )
    await run_content_brief(db, job, FakeLLM([json.dumps(BRIEF)]))

    llm = FakeLLM([json.dumps(_invalid_outline()), json.dumps(_valid_outline())])
    try:
        outline, repairs = await run_outline(
            db, job, llm, guideline_excerpt="G"
        )
    finally:
        await llm.aclose()

    assert repairs == 1
    assert validate_outline(outline) == []
    # the repair call carried the violation list (section 23)
    repair_call = llm.calls[1]["user"]
    assert "primary keyword" in repair_call
    assert "Previously generated outline" in repair_call
    row = db.scalars(select(ArticleOutlineRow)).first()
    assert row.repair_count == 1
    assert row.valid is True


async def test_outline_fails_after_two_repairs(db, job):
    await run_competitor_analysis(
        db, job, FakeLLM([_analysis_payload(i) for i in range(5)])
    )
    await run_serp_synthesis(db, job, FakeLLM([json.dumps(SYNTHESIS)]))
    await run_evidence_research(
        db, job, FakeLLM([json.dumps({"notes": EVIDENCE_NOTES})])
    )
    await run_content_brief(db, job, FakeLLM([json.dumps(BRIEF)]))

    llm = FakeLLM([json.dumps(_invalid_outline())] * 3)  # always invalid
    with pytest.raises(PipelineError) as ei:
        await run_outline(db, job, llm, guideline_excerpt="G")
    await llm.aclose()

    assert ei.value.error_code is ErrorCode.ARTICLE_VALIDATION_FAILED
    assert len(llm.calls) == 3  # 1 initial + 2 repairs
    # failed outline is NOT persisted as valid
    assert db.query(ArticleOutlineRow).count() == 0


async def test_outline_requires_brief(db, job):
    llm = FakeLLM([json.dumps(_valid_outline())])
    with pytest.raises(PipelineError):
        await run_outline(db, job, llm, guideline_excerpt="G")
    await llm.aclose()
