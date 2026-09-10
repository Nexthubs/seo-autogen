"""P4 acceptance demo: Research Pipeline (spec sections 16-18, 22, 23,
46.9-46.12; P4).

Everything is REAL:

  - real PostgreSQL (Docker ``seo-pg``) with migration 0005
    (competitor_analyses / serp_syntheses / evidence_notes /
     content_briefs / article_outlines)
  - real local LLM (litellm proxy at 127.0.0.1:4000, model
    qwen3.8-27b; .env values are Docker-perspective and are
    overridden here for host-side execution)
  - all 5 research steps end to end:
    CompetitorAnalyzer -> SERPSynthesis -> EvidenceResearch ->
    ContentBriefGenerator -> OutlineGenerator(+Validator/Repair)

Acceptance checklist (spec P4):
  [ ] CompetitorAnalyzer: one structured analysis per competitor
      source, Pydantic validated, JSONB persisted, prompt version
      recorded
  [ ] SERPSynthesis: 5 structured analyses + PAA + related in,
      synthesis JSONB persisted (one row per job)
  [ ] EvidenceResearch: factual notes persisted (competitor claims
      are not cited as evidence)
  [ ] ContentBriefGenerator: brief persisted (one row per job)
  [ ] OutlineGenerator: outline programmatic-validated
      (H2/H3 hierarchy, Practical, FAQ, CTA slot, required topics),
      repair path exercised on failure, max 2 repairs
  [ ] job status advanced through the P4 states to
      ARTICLE_GENERATING

Run:
  PYTHONPATH=/home/ubuntu/ai-coding/seo-autogen python3 scripts/p4_acceptance.py
"""

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete as sa_delete, select, text

from app.core.config import Settings
from app.core.enums import JobStatus
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
from app.db.session import SessionLocal, check_database
from app.pipeline.steps.competitor_analysis import run_competitor_analysis
from app.pipeline.steps.content_brief import run_content_brief
from app.pipeline.steps.evidence_research import run_evidence_research
from app.pipeline.steps.outline import run_outline
from app.pipeline.steps.serp_synthesis import run_serp_synthesis
from app.providers.llm.openai_compatible import OpenAICompatibleLLMProvider
from app.schemas.research import (
    ArticleOutline,
    CompetitorAnalysis,
    ContentBrief,
    EvidenceNote,
    SERPSynthesis,
)
from app.services.outline_validator import validate_outline
from app.services.prompt_service import seo_guideline_excerpt

ACC = "p4acc"
KEYWORD = "p4acc anxious attachment no contact"
MARKER = "P4ACC-[coach]"
SHEET = "p4acc-sheet"
URLS = [f"https://p4acc-site{i}.example.com/a{i}" for i in range(1, 6)]
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

CHECKS: list[tuple[str, bool]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((label, ok))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail and not ok else ""))


def build_settings() -> Settings:
    """Host-side LLM credentials (the .env points at Docker DNS)."""
    return Settings(
        llm_base_url=os.environ.get("P4_LLM_BASE_URL", "http://127.0.0.1:4000/v1"),
        llm_api_key=os.environ.get("P4_LLM_API_KEY", "sk-nexthubszhaozhao"),
        llm_model=os.environ.get("P4_LLM_MODEL", "qwen3.8-27b"),
    )


def reset() -> None:
    """Remove ONLY the acceptance rows — user data must survive."""
    with SessionLocal() as session:
        session.execute(
            text("DELETE FROM keywords WHERE cluster_id IN "
                 "(SELECT id FROM keyword_clusters WHERE sheet_name LIKE :s)"),
            {"s": f"{ACC}%"},
        )
        session.execute(
            text("DELETE FROM keyword_clusters WHERE sheet_name LIKE :s"),
            {"s": f"{ACC}%"},
        )
        session.execute(
            text("DELETE FROM internal_link_rules WHERE marker LIKE :m"),
            {"m": f"{ACC}%"},
        )
        session.execute(
            text("DELETE FROM generation_jobs WHERE keyword LIKE :p"),
            {"p": f"{ACC}%"},
        )
        session.commit()


def make_job() -> uuid.UUID:
    job_id = uuid.uuid4()
    with SessionLocal() as session:
        session.add(
            GenerationJob(
                id=job_id,
                keyword=KEYWORD,
                language="en",
                market="US",
                strategy="auto",
                status=JobStatus.SOURCE_EXTRACTING.value,
                current_step="source_extracting",
            )
        )
        session.flush()

        for i, url in enumerate(URLS, start=1):
            body = (
                f"# Competitor {i}: Anxious attachment and no contact\n\n"
                f"Article {i} about anxious attachment, no contact rules, "
                f"signs of an avoidant partner, and how to heal. "
            )
            body += ("Body paragraph. " * 60)
            page = SourcePage(
                url=url,
                normalized_url=url,
                url_hash=f"p4accuh{i:040d}",
                title=f"Competitor {i} article",
                domain=url.split("/")[2],
                content_markdown=body,
                content_hash=f"p4accch{i:040d}",
                extractor="test",
                first_seen_at=NOW,
                last_fetched_at=NOW,
            )
            session.add(page)
            session.flush()
            session.add(
                JobSource(job_id=job_id, source_page_id=page.id, serp_rank=i)
            )

        run = SerpRun(
            job_id=job_id,
            provider="acceptance",
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
                    title="Why does no contact work with anxious attachment?",
                    raw_item={},
                ),
                SerpResult(
                    serp_run_id=run.id,
                    result_type="related",
                    title="anxious attachment healing exercises",
                    raw_item={},
                ),
            ]
        )
        session.add(
            KeywordCluster(name=SHEET, sheet_name=SHEET)
        )
        session.flush()
        cluster = session.scalars(
            select(KeywordCluster).where(KeywordCluster.sheet_name == SHEET)
        ).one()
        session.add(
            Keyword(
                cluster_id=cluster.id,
                keyword=KEYWORD,
                volume=1200,
                kd=28.5,
                cpc=1.25,
                intent="Informational",
                source="acceptance",
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
    return job_id


async def run_pipeline(job_id: uuid.UUID, settings: Settings) -> None:
    provider = OpenAICompatibleLLMProvider(settings=settings)
    try:
        with SessionLocal() as session:
            job = session.get(GenerationJob, job_id)

            print("\n  -> CompetitorAnalyzer (5 sources, real LLM) ...")
            await run_competitor_analysis(session, job, provider)
            session.refresh(job)
            check(
                "status serp_analyzing after competitor analysis",
                job.status == JobStatus.SERP_ANALYZING.value,
                f"status={job.status}",
            )

            print("  -> SERPSynthesis (real LLM) ...")
            await run_serp_synthesis(session, job, provider)
            session.refresh(job)
            check(
                "status evidence_researching after synthesis",
                job.status == JobStatus.EVIDENCE_RESEARCHING.value,
                f"status={job.status}",
            )

            print("  -> EvidenceResearch (real LLM) ...")
            await run_evidence_research(session, job, provider)
            session.refresh(job)
            check(
                "status brief_generating after evidence",
                job.status == JobStatus.BRIEF_GENERATING.value,
                f"status={job.status}",
            )

            print("  -> ContentBriefGenerator (real LLM) ...")
            await run_content_brief(session, job, provider)
            session.refresh(job)
            check(
                "status outline_generating after brief",
                job.status == JobStatus.OUTLINE_GENERATING.value,
                f"status={job.status}",
            )

            print("  -> OutlineGenerator + validator (real LLM) ...")
            outline, repairs = await run_outline(
                session,
                job,
                provider,
                guideline_excerpt=seo_guideline_excerpt(settings),
            )
            session.refresh(job)
            check(
                "status article_generating after outline",
                job.status == JobStatus.ARTICLE_GENERATING.value,
                f"status={job.status}",
            )
            check(
                "outline passes programmatic validation",
                validate_outline(outline) == [],
                f"errors={validate_outline(outline)}",
            )
            check(
                "outline repair rounds <= 2",
                0 <= repairs <= 2,
                f"repairs={repairs}",
            )
    finally:
        await provider.aclose()


def verify_persistence(job_id: uuid.UUID) -> None:
    with SessionLocal() as session:
        rows = session.scalars(
            select(CompetitorAnalysisRow).where(
                CompetitorAnalysisRow.job_id == job_id
            )
        ).all()
        check("5 competitor analyses persisted", len(rows) == 5, f"n={len(rows)}")
        if rows:
            a = rows[0].analysis
            ok = (
                set(a)
                == {
                    "source_id", "content_type", "search_intent",
                    "estimated_word_count", "headings", "pain_points",
                    "key_topics", "practical_advice", "faq_topics",
                    "strengths", "weaknesses", "potential_gaps",
                }
                and isinstance(a["estimated_word_count"], int)
            )
            check("analysis JSONB matches section 16 shape", ok)
            check(
                "prompt version + model recorded on analysis",
                bool(rows[0].prompt_version) and bool(rows[0].model),
                f"v={rows[0].prompt_version} m={rows[0].model}",
            )
            # re-validate stored JSONB against the Pydantic model
            try:
                CompetitorAnalysis.model_validate(a)
                check("stored analysis re-validates (Pydantic)", True)
            except Exception as exc:
                check("stored analysis re-validates (Pydantic)", False, str(exc))

        syn = session.scalars(
            select(SerpSynthesisRow).where(SerpSynthesisRow.job_id == job_id)
        ).all()
        check("exactly one serp_syntheses row per job", len(syn) == 1, f"n={len(syn)}")
        if syn:
            check(
                "synthesis has dominant_intent",
                bool(syn[0].synthesis.get("dominant_intent")),
                f"dominant={syn[0].synthesis.get('dominant_intent')!r}",
            )

        ev = session.scalars(
            select(EvidenceNoteRow).where(EvidenceNoteRow.job_id == job_id)
        ).all()
        check("evidence notes persisted (>= 1)", len(ev) >= 1, f"n={len(ev)}")
        check(
            "evidence enums legal",
            {e.confidence for e in ev} <= {"high", "medium", "low"}
            and {e.usage for e in ev} <= {"supported", "soften", "avoid"},
        )

        briefs = session.scalars(
            select(ContentBriefRow).where(ContentBriefRow.job_id == job_id)
        ).all()
        check("exactly one content_briefs row per job", len(briefs) == 1, f"n={len(briefs)}")
        if briefs:
            b = ContentBrief.model_validate(briefs[0].brief)
            check(
                "brief primary_keyword matches the job",
                b.primary_keyword == KEYWORD,
                f"pk={b.primary_keyword!r}",
            )
            check(
                "brief word count is a positive int",
                b.recommended_word_count > 0,
                f"wc={b.recommended_word_count}",
            )

        outs = session.scalars(
            select(ArticleOutlineRow).where(ArticleOutlineRow.job_id == job_id)
        ).all()
        check("exactly one article_outlines row per job", len(outs) == 1, f"n={len(outs)}")
        if outs:
            o = ArticleOutline.model_validate(outs[0].outline)
            check("stored outline re-validates (Pydantic)", True)
            check("stored outline flagged valid", outs[0].valid is True)
            h2 = sum(1 for s in o.sections if s.level == 2)
            check(
                "6-12 H2 sections in stored outline",
                6 <= h2 <= 12,
                f"h2={h2}",
            )


def main() -> int:
    if not check_database():
        print("FATAL: PostgreSQL not reachable — cannot run P4 acceptance.")
        return 2

    settings = build_settings()
    print(f"LLM: {settings.llm_base_url} model={settings.llm_model}")

    async def _probe() -> bool:
        p = OpenAICompatibleLLMProvider(settings=settings)
        try:
            return await p.health_check()
        finally:
            await p.aclose()

    check(
        "LLM endpoint reachable (health_check)",
        asyncio.run(_probe()),
    )
    if not CHECKS[-1][1]:
        print("FATAL: LLM endpoint unreachable.")
        return 2

    reset()
    job_id = make_job()
    print(f"\nJob: {job_id}  keyword: {KEYWORD}")

    try:
        asyncio.run(run_pipeline(job_id, settings))
    except Exception as exc:  # pipeline failure -> acceptance fails
        check(f"pipeline ran to completion ({type(exc).__name__})", False, str(exc)[:300])
        print("\nPIPELINE FAILED — skipping persistence checks.")
        return 1

    print("\n== Persistence checks (46.9-46.12) ==")
    verify_persistence(job_id)

    passed = sum(1 for _, ok in CHECKS if ok)
    total = len(CHECKS)
    print(f"\n{'=' * 60}\nP4 ACCEPTANCE: {passed}/{total} checks passed")
    for label, ok in CHECKS:
        if not ok:
            print(f"  FAILED: {label}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
