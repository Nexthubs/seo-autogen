"""P5 acceptance demo: Article Generation + QA (spec sections 24-29,
46.13-46.14; P5).

Everything is REAL:

  - real PostgreSQL (Docker ``seo-pg``) with migration 0006
    (article_versions / article_reviews)
  - real local LLM (litellm proxy at 127.0.0.1:4000, model
    qwen3.8-27b; .env values are Docker-perspective and are
    overridden here for host-side execution)
  - P4 steps first (real LLM) to produce the Writer's input:
    CompetitorAnalyzer -> SERPSynthesis -> EvidenceResearch ->
    ContentBrief -> Outline(+Validator/Repair)
  - then all P5 steps end to end:
    ArticleWriter (v1) -> SEO/Fact/Style Review -> anti-copy check ->
    ArticleReviser (v2) -> final anti-copy check

Acceptance checklist (spec P5):
  [ ] ArticleDocument: title / body_markdown / seo_title /
      meta_description / slug — all present
  [ ] body_markdown 没有 # H1 (section 5 hard rule)
  [ ] local article.md export has EXACTLY ONE H1 (front matter + title)
  [ ] Writer version (v1) AND Revision version (v2) persisted, with
      model + prompt name + prompt version
  [ ] all review verdicts persisted (seo / fact / style / anticopy)
  [ ] anti-copy: report persisted; flags (if any) enter the reviser
  [ ] job status advanced through the P5 states to IMAGE_PLANNING

Run:
  PYTHONPATH=/home/ubuntu/ai-coding/seo-autogen python3 scripts/p5_acceptance.py
"""

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete as sa_delete, select

from app.core.config import Settings
from app.core.enums import JobStatus
from app.db.models import (
    GenerationJob,
    InternalLinkRule,
    Keyword,
    KeywordCluster,
)
from app.db.models.article import ArticleReviewRow, ArticleVersionRow
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
from app.pipeline.steps.article_reviser import run_article_reviser
from app.pipeline.steps.article_writer import run_article_writer
from app.pipeline.steps.competitor_analysis import run_competitor_analysis
from app.pipeline.steps.content_brief import run_content_brief
from app.pipeline.steps.evidence_research import run_evidence_research
from app.pipeline.steps.outline import run_outline
from app.pipeline.steps.reviewers import (
    run_fact_review,
    run_seo_review,
    run_style_review,
)
from app.pipeline.steps.serp_synthesis import run_serp_synthesis
from app.providers.llm.openai_compatible import OpenAICompatibleLLMProvider
from app.schemas.article import ArticleDocument
from app.services.article_renderer import export_article
from app.services.prompt_service import seo_guideline_excerpt

ACC = "p5acc"
KEYWORD = "p5acc anxious attachment no contact"
MARKER = "P5ACC-[coach]"
SHEET = "p5acc-sheet"
URLS = [f"https://p5acc-site{i}.example.com/a{i}" for i in range(1, 6)]
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
EXPORT_DIR = os.path.join(os.path.dirname(__file__), "_p5_export")

CHECKS: list[tuple[str, bool]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((label, ok))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail and not ok else ""))


def build_settings() -> Settings:
    """Host-side LLM credentials (the .env points at Docker DNS)."""
    return Settings(
        llm_base_url=os.environ.get("P5_LLM_BASE_URL", "http://127.0.0.1:4000/v1"),
        llm_api_key=os.environ.get("P5_LLM_API_KEY", "sk-nexthubszhaozhao"),
        llm_model=os.environ.get("P5_LLM_MODEL", "qwen3.8-27b"),
    )


def reset() -> None:
    """Remove ONLY the acceptance rows — user data must survive."""
    with SessionLocal() as session:
        session.execute(sa_delete(ArticleReviewRow).where(
            ArticleReviewRow.job_id.in_(
                select(GenerationJob.id).where(GenerationJob.keyword.ilike(f"{ACC}%"))
            )
        ))
        session.execute(sa_delete(ArticleVersionRow).where(
            ArticleVersionRow.job_id.in_(
                select(GenerationJob.id).where(GenerationJob.keyword.ilike(f"{ACC}%"))
            )
        ))
        session.execute(sa_delete(CompetitorAnalysisRow).where(
            CompetitorAnalysisRow.job_id.in_(
                select(GenerationJob.id).where(GenerationJob.keyword.ilike(f"{ACC}%"))
            )
        ))
        for model, col in (
            (ArticleOutlineRow, ArticleOutlineRow.job_id),
            (ContentBriefRow, ContentBriefRow.job_id),
            (EvidenceNoteRow, EvidenceNoteRow.job_id),
            (SerpSynthesisRow, SerpSynthesisRow.job_id),
        ):
            session.execute(sa_delete(model).where(
                col.in_(
                    select(GenerationJob.id).where(GenerationJob.keyword.ilike(f"{ACC}%"))
                )
            ))
        session.execute(sa_delete(SerpResult).where(
            SerpResult.serp_run_id.in_(
                select(SerpRun.id).where(SerpRun.job_id.in_(
                    select(GenerationJob.id).where(GenerationJob.keyword.ilike(f"{ACC}%"))
                ))
            )
        ))
        session.execute(sa_delete(SerpRun).where(
            SerpRun.job_id.in_(
                select(GenerationJob.id).where(GenerationJob.keyword.ilike(f"{ACC}%"))
            )
        ))
        session.execute(sa_delete(JobSource).where(
            JobSource.job_id.in_(
                select(GenerationJob.id).where(GenerationJob.keyword.ilike(f"{ACC}%"))
            )
        ))
        session.execute(sa_delete(SourcePage).where(
            SourcePage.url.in_(URLS)
        ))
        session.execute(sa_delete(InternalLinkRule).where(
            InternalLinkRule.marker.ilike(f"{ACC}%")
        ))
        session.execute(sa_delete(Keyword).where(
            Keyword.cluster_id.in_(
                select(KeywordCluster.id).where(KeywordCluster.sheet_name.ilike(f"{ACC}%"))
            )
        ))
        session.execute(sa_delete(KeywordCluster).where(
            KeywordCluster.sheet_name.ilike(f"{ACC}%")
        ))
        session.execute(sa_delete(GenerationJob).where(
            GenerationJob.keyword.ilike(f"{ACC}%")
        ))
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
            body += (f"Body paragraph {i} with unique filler words. " * 60)
            page = SourcePage(
                url=url,
                normalized_url=url,
                url_hash=f"p5accuh{i:040d}",
                title=f"Competitor {i} article",
                domain=url.split("/")[2],
                content_markdown=body,
                content_hash=f"p5accch{i:040d}",
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
        session.add(KeywordCluster(name=SHEET, sheet_name=SHEET))
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
            guideline = seo_guideline_excerpt(settings)

            # ---- P4: produce the Writer's input (real LLM) ----
            print("\n  -> [P4] CompetitorAnalyzer (5 sources) ...")
            await run_competitor_analysis(session, job, provider)
            print("  -> [P4] SERPSynthesis ...")
            await run_serp_synthesis(session, job, provider)
            print("  -> [P4] EvidenceResearch ...")
            await run_evidence_research(session, job, provider)
            print("  -> [P4] ContentBriefGenerator ...")
            await run_content_brief(session, job, provider)
            print("  -> [P4] OutlineGenerator + validator ...")
            outline, repairs = await run_outline(
                session, job, provider, guideline_excerpt=guideline
            )
            session.refresh(job)
            check(
                "P4 outputs ready: status article_generating",
                job.status == JobStatus.ARTICLE_GENERATING.value,
                f"status={job.status}",
            )
            check("P4 outline valid, repairs <= 2", 0 <= repairs <= 2,
                  f"repairs={repairs}")

            # ---- P5: writer -> reviews -> revision ----
            print("  -> [P5] ArticleWriter (v1) ...")
            doc = await run_article_writer(
                session, job, provider, guideline_excerpt=guideline
            )
            session.refresh(job)
            check(
                "status seo_reviewing after writer",
                job.status == JobStatus.SEO_REVIEWING.value,
                f"status={job.status}",
            )
            # ArticleDocument checks (section 24 + section 5)
            check(
                "ArticleDocument fields present",
                bool(doc.title) and bool(doc.body_markdown)
                and bool(doc.seo_title) and bool(doc.meta_description)
                and bool(doc.slug),
                f"title={doc.title[:50]!r}",
            )
            check(
                "body_markdown has NO H1 (section 5)",
                "\n# " not in ("\n" + doc.body_markdown),
            )

            print("  -> [P5] SEOReviewer ...")
            seo = await run_seo_review(session, job, provider,
                                       guideline_excerpt=guideline)
            session.refresh(job)
            check("status fact_reviewing after SEO review",
                  job.status == JobStatus.FACT_REVIEWING.value,
                  f"status={job.status}")
            print(f"     seo total={seo.total_score} issues={len(seo.issues)}")

            print("  -> [P5] FactReviewer ...")
            fact = await run_fact_review(session, job, provider,
                                         guideline_excerpt=guideline)
            session.refresh(job)
            check("status style_reviewing after fact review",
                  job.status == JobStatus.STYLE_REVIEWING.value,
                  f"status={job.status}")
            print(f"     fact issues={len(fact.issues)}")

            print("  -> [P5] StyleReviewer ...")
            style = await run_style_review(session, job, provider,
                                           guideline_excerpt=guideline)
            session.refresh(job)
            check("status article_revising after style review",
                  job.status == JobStatus.ARTICLE_REVISING.value,
                  f"status={job.status}")
            print(f"     style score={style.score}")

            print("  -> [P5] ArticleReviser (v2 + anti-copy both versions) ...")
            revised, final_anticopy = await run_article_reviser(
                session, job, provider
            )
            session.refresh(job)
            check(
                "status image_planning after revision (P5 end)",
                job.status == JobStatus.IMAGE_PLANNING.value,
                f"status={job.status}",
            )
            check(
                "final anti-copy report produced",
                final_anticopy.sources_compared == 5,
                f"sources={final_anticopy.sources_compared}",
            )
            print(f"     final anti-copy matches={len(final_anticopy.matches)}")

            # local article.md export (section 5 / P5 DoD)
            export_path = os.path.join(EXPORT_DIR, "article.md")
            export_article(
                export_path,
                title=doc.title,
                body_markdown=doc.body_markdown,
                seo_title=doc.seo_title,
                meta_description=doc.meta_description,
                slug=doc.slug,
            )
            text = open(export_path, encoding="utf-8").read()
            h1_lines = [l for l in text.splitlines()
                        if l.startswith("# ") or l == "#"]
            check(
                "exported article.md has exactly ONE H1",
                h1_lines == [f"# {doc.title}"],
                f"h1={h1_lines[:2]}",
            )
            check(
                "article.md front matter present",
                text.startswith("---\nmetaTitle:")
                and "\nslug: " in text.split("---")[1],
            )
    finally:
        await provider.aclose()


def verify_persistence(job_id: uuid.UUID) -> None:
    with SessionLocal() as session:
        versions = session.scalars(
            select(ArticleVersionRow)
            .where(ArticleVersionRow.job_id == job_id)
            .order_by(ArticleVersionRow.version)
        ).all()
        check(
            "Writer v1 + Revision v2 both persisted",
            [v.version for v in versions] == [1, 2]
            and [v.stage for v in versions] == ["writer", "revision"],
            f"versions={[(v.version, v.stage) for v in versions]}",
        )
        for v in versions:
            check(
                f"v{v.version} records model + prompt name/version",
                bool(v.model) and bool(v.prompt_name) and bool(v.prompt_version),
                f"m={v.model} p={v.prompt_name} pv={v.prompt_version}",
            )
            stored = session.get(ArticleVersionRow, v.id).article_document
            check(
                f"v{v.version} stored doc re-validates (ArticleDocument)",
                ArticleDocument(
                    **stored,
                    primary_keyword=KEYWORD,
                    search_intent="informational",
                    article_strategy="guide",
                ) is not None,
            )

        reviews = session.scalars(
            select(ArticleReviewRow).where(
                ArticleReviewRow.job_id == job_id
            )
        ).all()
        types = {(r.article_version_id, r.review_type) for r in reviews}
        if len(versions) == 2:
            v1, v2 = versions
            check("seo review persisted on v1", (v1.id, "seo") in types)
            check("fact review persisted on v1", (v1.id, "fact") in types)
            check("style review persisted on v1", (v1.id, "style") in types)
            check("anticopy review persisted on v1", (v1.id, "anticopy") in types)
            check("anticopy review persisted on v2 (final)",
                  (v2.id, "anticopy") in types)
        sev = [r for r in reviews if r.review_type == "seo"]
        if sev:
            check("seo review records model + prompt version",
                  bool(sev[0].model) and bool(sev[0].prompt_version),
                  f"m={sev[0].model} pv={sev[0].prompt_version}")
            check("seo review has the 6 section-26.1 scores",
                  all(k in sev[0].review for k in (
                      "total_score", "keyword_score",
                      "search_intent_score", "structure_score",
                      "readability_score", "cta_score")),
            )


def main() -> int:
    if not check_database():
        print("FATAL: PostgreSQL not reachable — cannot run P5 acceptance.")
        return 2

    settings = build_settings()
    print(f"LLM: {settings.llm_base_url} model={settings.llm_model}")

    async def _probe() -> bool:
        p = OpenAICompatibleLLMProvider(settings=settings)
        try:
            return await p.health_check()
        finally:
            await p.aclose()

    check("LLM endpoint reachable (health_check)", asyncio.run(_probe()))
    if not CHECKS[-1][1]:
        print("FATAL: LLM endpoint unreachable.")
        return 2

    reset()
    job_id = make_job()
    print(f"\nJob: {job_id}  keyword: {KEYWORD}")

    try:
        asyncio.run(run_pipeline(job_id, settings))
    except Exception as exc:  # pipeline failure -> acceptance fails
        check(f"pipeline ran to completion ({type(exc).__name__})",
              False, str(exc)[:300])
        print("\nPIPELINE FAILED — skipping persistence checks.")
        return 1

    print("\n== Persistence checks (46.13-46.14) ==")
    verify_persistence(job_id)

    passed = sum(1 for _, ok in CHECKS if ok)
    total = len(CHECKS)
    print(f"\n{'=' * 60}\nP5 ACCEPTANCE: {passed}/{total} checks passed")
    for label, ok in CHECKS:
        if not ok:
            print(f"  FAILED: {label}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
