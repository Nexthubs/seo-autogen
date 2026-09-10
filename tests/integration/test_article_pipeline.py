"""P5 integration: article pipeline (spec sections 24-29, 46.13-46.14).

Runs against the local PostgreSQL (docker container) with a scripted
fake LLM (no external LLM). Skipped when the database is unreachable.

Covers:
  - article_versions / article_reviews table shapes (spec 46.13-46.14)
  - UNIQUE (job_id, version) constraint on article_versions
  - writer -> seo/fact/style reviews -> revision full run
  - versioning: v1 (writer) + v2 (revision) coexist, never overwritten
  - reviews persisted per (article_version_id, review_type)
  - job status chain through the P5 states, ending at IMAGE_PLANNING

Shared integration DB discipline: every test only creates rows with
``p5test`` markers and deletes exactly those rows in teardown.
"""

import json

import pytest
from sqlalchemy import delete as sa_delete, inspect, select, text

from app.core.enums import JobStatus
from app.db.models import GenerationJob, InternalLinkRule
from app.db.models.article import ArticleReviewRow, ArticleVersionRow
from app.db.models.research import (
    ArticleOutlineRow,
    ContentBriefRow,
    EvidenceNoteRow,
    SerpSynthesisRow,
)
from app.db.models.source import JobSource, SourcePage
from app.db.session import SessionLocal, check_database, engine
from app.pipeline.steps.article_reviser import run_article_reviser
from app.pipeline.steps.article_writer import run_article_writer
from app.services.prompt_service import load_prompt
from app.pipeline.steps.reviewers import (
    run_fact_review,
    run_seo_review,
    run_style_review,
)

from tests.unit.test_article_steps import (
    BRIEF,
    COPIED_SENTENCE,
    FACT_REVIEW,
    REVISER_DRAFT,
    SEO_REVIEW,
    STYLE_REVIEW,
    WRITER_DRAFT,
    FakeLLM,
)

from datetime import datetime, timezone

pytestmark = pytest.mark.skipif(
    not check_database(), reason="PostgreSQL not reachable"
)

KEYWORD = "p5test anxious attachment no contact"
MARKER = "[p5coach]"
URLS = [f"https://p5test-site{i}.example.com/a{i}" for i in range(1, 6)]
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

BRIEF_P5 = {**BRIEF, "primary_keyword": KEYWORD, "internal_link_markers": [MARKER]}
OUTLINE_P5 = {
    "title": f"{KEYWORD.title()}: A Complete Practical Guide",
    "sections": [
        {"heading": "What is avoidant attachment", "level": 2,
         "purpose": "x", "keywords": ["avoidant attachment"], "cta_slot": False},
        {"heading": "Practical steps", "level": 2,
         "purpose": "x", "keywords": ["no contact"], "cta_slot": True},
    ],
    "faq_questions": ["q1?"],
}
SYNTHESIS_P5 = {"dominant_intent": "informational", "common_topics": ["no contact"]}


# ============================================================
# fixtures / cleanup
# ============================================================
@pytest.fixture()
def env():
    """Job + 5 sources + P4 outputs + one active link rule (p5test)."""
    job_id = None
    with SessionLocal() as session:
        job = GenerationJob(
            keyword=KEYWORD,
            status=JobStatus.OUTLINE_GENERATING.value,
            target_function="coach",
        )
        session.add(job)
        session.flush()
        job_id = job.id

        pages = []
        for i, url in enumerate(URLS, start=1):
            content = f"# Competitor {i}\n\n" + (
                ("Intro here. " + COPIED_SENTENCE + " Outro here. ")
                if i == 1
                else f"Unrelated body text for competitor number {i}. "
            )
            content += f"Filler {i}. " * 15
            page = SourcePage(
                url=url,
                normalized_url=url,
                url_hash=f"p5testuh{i:036d}",
                title=f"Competitor {i}",
                domain=url.split("/")[2],
                content_markdown=content,
                content_hash=f"p5testch{i:036d}",
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

        # P4 outputs — seeded directly so P5 can run standalone.
        session.add(ContentBriefRow(job_id=job.id, brief=BRIEF_P5))
        session.add(
            ArticleOutlineRow(
                job_id=job.id, outline=OUTLINE_P5,
                valid=True, repair_count=0,
            )
        )
        session.add(SerpSynthesisRow(job_id=job.id, synthesis=SYNTHESIS_P5))
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
        yield {"job_id": job_id}
    finally:
        _cleanup(job_id)


def _cleanup(job_id) -> None:
    """Delete exactly the rows this fixture created (p5test scope)."""
    with SessionLocal() as session:
        session.execute(sa_delete(ArticleReviewRow).where(
            ArticleReviewRow.job_id == job_id
        ))
        session.execute(sa_delete(ArticleVersionRow).where(
            ArticleVersionRow.job_id == job_id
        ))
        session.execute(sa_delete(ArticleOutlineRow).where(
            ArticleOutlineRow.job_id == job_id
        ))
        session.execute(sa_delete(ContentBriefRow).where(
            ContentBriefRow.job_id == job_id
        ))
        session.execute(sa_delete(EvidenceNoteRow).where(
            EvidenceNoteRow.job_id == job_id
        ))
        session.execute(sa_delete(SerpSynthesisRow).where(
            SerpSynthesisRow.job_id == job_id
        ))
        session.execute(sa_delete(JobSource).where(JobSource.job_id == job_id))
        session.execute(sa_delete(SourcePage).where(SourcePage.url.in_(URLS)))
        session.execute(sa_delete(InternalLinkRule).where(
            InternalLinkRule.marker == MARKER
        ))
        session.execute(sa_delete(GenerationJob).where(GenerationJob.id == job_id))
        session.commit()


async def _full_run(env) -> None:
    """writer -> 3 reviews -> reviser, asserting the status chain."""
    with SessionLocal() as session:
        job = session.get(GenerationJob, env["job_id"])

        llm = FakeLLM([json.dumps(WRITER_DRAFT)])
        doc = await run_article_writer(
            session, job, llm, guideline_excerpt="INTEGRATION GUIDELINE"
        )
        await llm.aclose()
        session.refresh(job)
        assert job.status == JobStatus.SEO_REVIEWING.value

        llm = FakeLLM([json.dumps(SEO_REVIEW)])
        await run_seo_review(session, job, llm, guideline_excerpt="G")
        await llm.aclose()
        session.refresh(job)
        assert job.status == JobStatus.FACT_REVIEWING.value

        llm = FakeLLM([json.dumps(FACT_REVIEW)])
        await run_fact_review(session, job, llm, guideline_excerpt="G")
        await llm.aclose()
        session.refresh(job)
        assert job.status == JobStatus.STYLE_REVIEWING.value

        llm = FakeLLM([json.dumps(STYLE_REVIEW)])
        await run_style_review(session, job, llm, guideline_excerpt="G")
        await llm.aclose()
        session.refresh(job)
        assert job.status == JobStatus.ARTICLE_REVISING.value

        llm = FakeLLM([json.dumps(REVISER_DRAFT)])
        revised, final = await run_article_reviser(session, job, llm)
        await llm.aclose()
        session.refresh(job)
        assert job.status == JobStatus.IMAGE_PLANNING.value


# ============================================================
# table shapes (spec 46.13-46.14)
# ============================================================
def test_table_shapes_match_spec(env):
    def cols(table: str) -> set[str]:
        return {c["name"] for c in inspect(engine).get_columns(table)}

    assert cols("article_versions") >= {
        "id", "job_id", "version", "stage", "title", "body_markdown",
        "seo_title", "meta_description", "slug", "model", "prompt_name",
        "prompt_version", "prompt_hash", "created_at",
    }
    assert cols("article_reviews") >= {
        "id", "job_id", "article_version_id", "review_type", "review",
        "model", "prompt_version", "prompt_hash", "created_at",
    }


def test_unique_job_id_version_constraint(env):
    with SessionLocal() as session:
        rows = session.execute(
            text(
                "SELECT con.conname, "
                "ARRAY(SELECT a.attname FROM unnest(con.conkey) k "
                "JOIN pg_attribute a ON a.attrelid = con.conrelid "
                "AND a.attnum = k) AS cols "
                "FROM pg_constraint con "
                "JOIN pg_class c ON c.oid = con.conrelid "
                "WHERE c.relname = 'article_versions' AND con.contype = 'u'"
            )
        ).fetchall()
        defs = {tuple(r.cols) for r in rows}
        assert ("job_id", "version") in defs, defs


# ============================================================
# full pipeline run
# ============================================================
async def test_full_pipeline_versioning_and_reviews(env):
    await _full_run(env)

    with SessionLocal() as session:
        versions = session.scalars(
            select(ArticleVersionRow)
            .where(ArticleVersionRow.job_id == env["job_id"])
            .order_by(ArticleVersionRow.version)
        ).all()
        assert [v.version for v in versions] == [1, 2]
        assert [v.stage for v in versions] == ["writer", "revision"]
        v1, v2 = versions

        # v1 untouched by the revision; H1-free; title from the outline
        assert "\n# " not in ("\n" + v1.body_markdown)
        assert v1.title == OUTLINE_P5["title"]
        assert v2.title == OUTLINE_P5["title"]
        assert v1.body_markdown != v2.body_markdown
        assert v1.prompt_name == "article_writer"
        assert v2.prompt_name == "article_reviser"
        assert v1.model == "test-model" and v2.model == "test-model"
        # P9-B2: recorded hashes match the on-disk files (spec 48)
        assert v1.prompt_hash == load_prompt("article_writer").prompt_hash
        assert v2.prompt_hash == load_prompt("article_reviser").prompt_hash

        # reviews: seo/fact/style on v1, anticopy on v1 AND v2
        reviews = session.scalars(
            select(ArticleReviewRow).where(
                ArticleReviewRow.job_id == env["job_id"]
            )
        ).all()
        by_version_type = {
            (r.article_version_id, r.review_type): r for r in reviews
        }
        assert (v1.id, "seo") in by_version_type
        assert (v1.id, "fact") in by_version_type
        assert (v1.id, "style") in by_version_type
        assert (v1.id, "anticopy") in by_version_type
        assert (v2.id, "anticopy") in by_version_type
        # the written draft overlaps source 1; the revision does not
        assert by_version_type[(v1.id, "anticopy")].review["has_serious_overlap"]
        assert not by_version_type[(v2.id, "anticopy")].review["has_serious_overlap"]
        # reviews carry provenance (incl. the prompt hash, P9-B2)
        assert by_version_type[(v1.id, "seo")].model == "test-model"
        assert by_version_type[(v1.id, "seo")].prompt_version == "1.0"
        assert by_version_type[(v1.id, "seo")].prompt_hash == (
            load_prompt("seo_reviewer").prompt_hash
        )
        assert by_version_type[(v1.id, "fact")].prompt_hash == (
            load_prompt("fact_reviewer").prompt_hash
        )


async def test_version_uniqueness_enforced(env):
    """Inserting a second v1 row for the same job must fail."""
    with SessionLocal() as session:
        job = session.get(GenerationJob, env["job_id"])
        llm = FakeLLM([json.dumps(WRITER_DRAFT)])
        await run_article_writer(session, job, llm, guideline_excerpt="G")
        await llm.aclose()
        # manually insert a duplicate (job_id, version)
        dup = ArticleVersionRow(
            job_id=job.id,
            version=1,
            stage="writer",
            title="dup",
            body_markdown="## s\n\nx",
            seo_title="x",
            meta_description="x",
            slug="dup",
        )
        session.add(dup)
        with pytest.raises(Exception) as ei:
            session.commit()
        assert "uq" in str(ei.value).lower() or "unique" in str(ei.value).lower()
        session.rollback()
