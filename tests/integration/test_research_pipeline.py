"""P4 integration: research pipeline (spec sections 9, 16-18, 22, 23,
46.9-46.12).

Runs against the local PostgreSQL (docker container) with a scripted
fake LLM (no external LLM). Skipped when the database is unreachable.

Covers:
  - table shapes match spec 46.9-46.12 (+ serp_syntheses)
  - full 5-step run: competitor analysis -> SERP synthesis ->
    evidence research -> content brief -> outline
  - job status transitions through the P4 states
  - outline repair path (invalid first, repaired second)
  - re-run of synthesis/brief/outline replaces rows (UNIQUE job_id)

Shared integration DB discipline: every test only creates rows with
``p4test`` markers and deletes exactly those rows in teardown.
"""

import uuid

import pytest
from sqlalchemy import delete as sa_delete, inspect, select, text

from app.core.enums import JobStatus
from app.db.models import (
    GenerationJob,
    InternalLinkRule,
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
from app.db.session import SessionLocal, check_database, engine
from app.pipeline.steps.competitor_analysis import run_competitor_analysis
from app.pipeline.steps.content_brief import run_content_brief
from app.pipeline.steps.evidence_research import run_evidence_research
from app.pipeline.steps.outline import run_outline
from app.pipeline.steps.serp_synthesis import run_serp_synthesis
from app.services.prompt_service import load_prompt

from tests.unit.test_research_steps import (
    ANALYSIS_TMPL,
    BRIEF,
    EVIDENCE_NOTES,
    FakeLLM,
    SYNTHESIS,
    _invalid_outline,
    _valid_outline,
)

import json
from datetime import datetime, timezone

pytestmark = pytest.mark.skipif(
    not check_database(), reason="PostgreSQL not reachable"
)

KEYWORD = "p4test anxious attachment no contact"
MARKER = "[p4coach]"
URLS = [f"https://p4test-site{i}.example.com/a{i}" for i in range(1, 6)]
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ============================================================
# fixtures / cleanup
# ============================================================
@pytest.fixture()
def env():
    """Job + 5 sources + SERP run + one active link rule (p4test scope)."""
    job_id = None
    with SessionLocal() as session:
        job = GenerationJob(
            keyword=KEYWORD,
            status=JobStatus.SOURCE_EXTRACTING.value,
            target_function="coach",
        )
        session.add(job)
        session.flush()
        job_id = job.id

        pages = []
        for i, url in enumerate(URLS, start=1):
            page = SourcePage(
                url=url,
                normalized_url=url,
                url_hash=f"p4testuh{i:040d}",
                title=f"Competitor {i}",
                domain=url.split("/")[2],
                content_markdown=f"# Competitor {i}\n\nBody text {i}. " * 20,
                content_hash=f"p4testch{i:040d}",
                extractor="test",
                first_seen_at=NOW,
                last_fetched_at=NOW,
            )
            session.add(page)
            session.flush()
            pages.append(page)
            session.add(
                JobSource(job_id=job.id, source_page_id=page.id, serp_rank=i)
            )

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
        session.add(
            InternalLinkRule(
                marker=MARKER,
                anchor_text="book a call",
                target_url="/coach",
                keywords=["coach"],
                active=True,
            )
        )
        session.commit()

    try:
        yield {"job_id": job_id, "page_ids": [p.id for p in pages]}
    finally:
        _cleanup(job_id, env_urls=URLS)


def _cleanup(job_id: uuid.UUID, env_urls: list[str]) -> None:
    """Delete exactly the rows this fixture created (p4test scope)."""
    with SessionLocal() as session:
        session.execute(
            sa_delete(ArticleOutlineRow).where(ArticleOutlineRow.job_id == job_id)
        )
        session.execute(
            sa_delete(ContentBriefRow).where(ContentBriefRow.job_id == job_id)
        )
        session.execute(
            sa_delete(EvidenceNoteRow).where(EvidenceNoteRow.job_id == job_id)
        )
        session.execute(
            sa_delete(SerpSynthesisRow).where(SerpSynthesisRow.job_id == job_id)
        )
        session.execute(
            sa_delete(CompetitorAnalysisRow).where(
                CompetitorAnalysisRow.job_id == job_id
            )
        )
        session.execute(
            sa_delete(SerpResult).where(
                SerpResult.serp_run_id.in_(
                    select(SerpRun.id).where(SerpRun.job_id == job_id)
                )
            )
        )
        session.execute(sa_delete(SerpRun).where(SerpRun.job_id == job_id))
        session.execute(sa_delete(JobSource).where(JobSource.job_id == job_id))
        session.execute(
            sa_delete(SourcePage).where(SourcePage.url.in_(env_urls))
        )
        session.execute(
            sa_delete(InternalLinkRule).where(InternalLinkRule.marker == MARKER)
        )
        session.execute(sa_delete(GenerationJob).where(GenerationJob.id == job_id))
        session.commit()


async def _fake_llm(payloads) -> FakeLLM:
    return FakeLLM([json.dumps(p) for p in payloads])


async def _run_research(env, llm) -> tuple:
    """Run the 5 research steps in order; returns their outputs."""
    with SessionLocal() as session:
        job = session.get(GenerationJob, env["job_id"])
        analyses = await run_competitor_analysis(session, job, llm)
        session.refresh(job)
        assert job.status == JobStatus.SERP_ANALYZING.value
        syn = await run_serp_synthesis(session, job, llm)
        session.refresh(job)
        assert job.status == JobStatus.EVIDENCE_RESEARCHING.value
        notes = await run_evidence_research(session, job, llm)
        session.refresh(job)
        assert job.status == JobStatus.BRIEF_GENERATING.value
        brief = await run_content_brief(session, job, llm)
        session.refresh(job)
        assert job.status == JobStatus.OUTLINE_GENERATING.value
        outline, repairs = await run_outline(
            session, job, llm, guideline_excerpt="INTEGRATION GUIDELINE"
        )
        session.refresh(job)
        assert job.status == JobStatus.ARTICLE_GENERATING.value
    return analyses, syn, notes, brief, outline, repairs


BRIEF_P4 = {**BRIEF, "primary_keyword": KEYWORD, "internal_link_markers": [MARKER]}


def _p4_outline(valid: bool) -> dict:
    o = _valid_outline() if valid else _invalid_outline()
    if valid:
        # the validator requires the primary keyword in the title
        o = dict(o)
        o["title"] = f"{KEYWORD.title()}: A Complete Practical Guide"
    return o


def _payloads(valid=True) -> list:
    outlines = [_p4_outline(valid)]
    return (
        [ANALYSIS_TMPL] * 5
        + [SYNTHESIS]
        + [{"notes": EVIDENCE_NOTES}]
        + [BRIEF_P4]
        + outlines
    )


# ============================================================
# table shapes (spec 46.9-46.12)
# ============================================================
def test_table_shapes_match_spec(env):
    with SessionLocal() as session:
        def cols(table: str) -> set[str]:
            return {c["name"] for c in inspect(engine).get_columns(table)}

        assert cols("competitor_analyses") >= {
            "id", "job_id", "source_page_id", "analysis", "model",
            "prompt_version", "prompt_hash", "created_at",
        }
        assert cols("serp_syntheses") >= {
            "id", "job_id", "synthesis", "model", "prompt_version", "prompt_hash", "created_at",
        }
        assert cols("evidence_notes") >= {
            "id", "job_id", "claim", "source_title", "source_url", "source_type",
            "confidence", "usage", "note", "created_at",
        }
        assert cols("content_briefs") >= {
            "id", "job_id", "brief", "model", "prompt_version", "prompt_hash", "created_at",
        }
        assert cols("article_outlines") >= {
            "id", "job_id", "outline", "model", "prompt_version", "prompt_hash", "valid",
            "repair_count", "created_at",
        }

        # UNIQUE job_id constraints (3 rows-per-job tables)
        for table in ("serp_syntheses", "content_briefs", "article_outlines"):
            with session.bind.connect() as conn:
                idx = conn.execute(
                    text(
                        "SELECT a.attname FROM pg_index i "
                        "JOIN pg_attribute a ON a.attrelid = i.indrelid "
                        "AND a.attnum = ANY(i.indkey) "
                        f"WHERE i.indrelid = '{table}'::regclass AND i.indisunique"
                    )
                ).fetchall()
            names = {r[0] for r in idx}
            assert "job_id" in names, table


# ============================================================
# full pipeline
# ============================================================
async def test_full_research_pipeline(env):
    llm = await _fake_llm(_payloads(valid=True))
    try:
        analyses, syn, notes, brief, outline, repairs = await _run_research(env, llm)
    finally:
        await llm.aclose()

    assert len(analyses) == 5
    assert repairs == 0

    with SessionLocal() as session:
        rows = session.scalars(
            select(CompetitorAnalysisRow).where(
                CompetitorAnalysisRow.job_id == env["job_id"]
            )
        ).all()
        assert len(rows) == 5
        for r in rows:
            a = r.analysis
            # Pydantic-validated JSONB (section 16)
            assert set(a) == {
                "source_id", "content_type", "search_intent",
                "estimated_word_count", "headings", "pain_points",
                "key_topics", "practical_advice", "faq_topics",
                "strengths", "weaknesses", "potential_gaps",
            }
            assert isinstance(a["estimated_word_count"], int)
            assert r.model == "test-model"
            assert r.prompt_version == "1.0"
            # P9-B2: the recorded hash matches the on-disk file (spec 48)
            assert r.prompt_hash == load_prompt("competitor_analyzer").prompt_hash
            assert r.source_page_id in env["page_ids"]

        syn_row = session.scalars(
            select(SerpSynthesisRow).where(
                SerpSynthesisRow.job_id == env["job_id"]
            )
        ).one()
        assert syn_row.synthesis["dominant_intent"] == "informational"

        ev = session.scalars(
            select(EvidenceNoteRow).where(
                EvidenceNoteRow.job_id == env["job_id"]
            )
        ).all()
        assert len(ev) == len(EVIDENCE_NOTES)
        assert {e.confidence for e in ev} <= {"high", "medium", "low"}
        assert {e.usage for e in ev} <= {"supported", "soften", "avoid"}

        brief_row = session.scalars(
            select(ContentBriefRow).where(
                ContentBriefRow.job_id == env["job_id"]
            )
        ).one()
        assert brief_row.brief["primary_keyword"] == KEYWORD
        assert brief_row.brief["internal_link_markers"] == [MARKER]

        out_row = session.scalars(
            select(ArticleOutlineRow).where(
                ArticleOutlineRow.job_id == env["job_id"]
            )
        ).one()
        assert out_row.valid is True
        assert out_row.repair_count == 0
        assert out_row.outline["title"] == outline.title


async def test_outline_repair_path(env):
    """Invalid first outline -> one repair round -> valid, repair_count=1."""
    payloads = _payloads(valid=False)
    payloads.append(_p4_outline(True))
    llm = await _fake_llm(payloads)
    try:
        _, _, _, _, _, repairs = await _run_research(env, llm)
    finally:
        await llm.aclose()
    assert repairs == 1
    with SessionLocal() as session:
        row = session.scalars(
            select(ArticleOutlineRow).where(
                ArticleOutlineRow.job_id == env["job_id"]
            )
        ).one()
        assert row.valid is True
        assert row.repair_count == 1


async def test_research_steps_are_rerunnable(env):
    """Re-running a step replaces its row (UNIQUE job_id tables)."""
    llm = await _fake_llm(_payloads(valid=True))
    try:
        await _run_research(env, llm)
    finally:
        await llm.aclose()

    # run the synthesis step once more
    llm2 = await _fake_llm([SYNTHESIS])
    try:
        with SessionLocal() as session:
            job = session.get(GenerationJob, env["job_id"])
            await run_serp_synthesis(session, job, llm2)
    finally:
        await llm2.aclose()

    with SessionLocal() as session:
        assert (
            session.scalars(
                select(SerpSynthesisRow).where(
                    SerpSynthesisRow.job_id == env["job_id"]
                )
            ).all().__len__()
            == 1
        )
        assert (
            session.scalars(
                select(CompetitorAnalysisRow).where(
                    CompetitorAnalysisRow.job_id == env["job_id"]
                )
            ).all().__len__()
            == 5
        )
