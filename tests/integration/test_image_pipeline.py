"""P6 integration: image pipeline (spec sections 30-34, 46.15).

Runs against the local PostgreSQL (docker container) with a scripted
fake LLM (planner) and a fake image provider (generation) — no
external LLM, no image API. Skipped when the database is unreachable.

Covers:
  - ``images`` table shape (spec 46.15), incl. strapi_* columns
    created nullable and left NULL in P6
  - planner -> generation full run: status chain
    image_planning -> image_generating -> ready
  - section 34 layout on disk (data/articles/{job}/images/*.webp,
    article.md + article.json)
  - hero never in the body with the default flag
  - re-run replaces the job's image rows (no orphans)

Shared integration DB discipline: every test only creates rows with
``p6test`` markers and deletes exactly those rows in teardown.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete as sa_delete, inspect, select, text

from app.core.config import Settings
from app.core.enums import JobStatus
from app.db.models import GenerationJob
from app.db.models.article import ArticleVersionRow
from app.db.models.images import ImageRow
from app.db.session import SessionLocal, check_database, engine
from app.pipeline.steps.image_generate import run_image_generation
from app.pipeline.steps.image_plan import run_image_planner

from tests.unit.test_image_steps import (
    PLAN_HERO_ONLY,
    PLAN_THREE,
    FakeImageProvider,
    FakeLLM,
)

import json

pytestmark = pytest.mark.skipif(
    not check_database(), reason="PostgreSQL not reachable"
)

KEYWORD = "p6test anxious attachment no contact"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

BODY_LONG = (
    "## What is anxious attachment\n\n"
    + "A real paragraph that explains the concept here in detail. " * 350
)


def _settings(data_dir: str) -> Settings:
    return Settings(
        llm_base_url="http://llm.test/v1",
        llm_api_key="test-key",
        llm_model="test-model",
        llm_max_retries=2,
        image_model="gpt-image-2",
        data_dir=data_dir,
        strapi_frontend_renders_main_image=True,
        _env_file=None,
    )


@pytest.fixture()
def env(tmp_path):
    """Job + one article version + fake settings (p6test scope)."""
    job_id = None
    with SessionLocal() as session:
        job = GenerationJob(
            keyword=KEYWORD,
            status=JobStatus.IMAGE_PLANNING.value,
            current_step="image_planning",
            target_function="coach",
        )
        session.add(job)
        session.flush()
        job_id = job.id
        session.add(
            ArticleVersionRow(
                job_id=job.id,
                version=1,
                stage="revision",
                title="P6test Anxious Attachment Guide",
                body_markdown=BODY_LONG,
                seo_title="P6 SEO title",
                meta_description="P6 meta.",
                slug="p6test-anxious-attachment",
                model="test-model",
                prompt_name="article_reviser",
                prompt_version="1.0",
            )
        )
        session.commit()
    try:
        yield {"job_id": job_id, "settings": _settings(str(tmp_path))}
    finally:
        _cleanup(job_id)


def _cleanup(job_id) -> None:
    """Delete exactly the rows this fixture created (p6test scope)."""
    with SessionLocal() as session:
        session.execute(sa_delete(ImageRow).where(ImageRow.job_id == job_id))
        session.execute(sa_delete(ArticleVersionRow).where(
            ArticleVersionRow.job_id == job_id
        ))
        session.execute(
            sa_delete(GenerationJob).where(GenerationJob.id == job_id)
        )
        session.commit()


def test_images_table_shape_section46_15():
    """Spec 46.15: every column exists; strapi_* are nullable."""
    columns = {c["name"]: c for c in inspect(engine).get_columns("images")}
    for name in (
        "id", "job_id", "role", "sort_order", "purpose",
        "section_heading", "insertion_marker", "prompt", "filename",
        "alt_text", "aspect_ratio", "local_path", "mime_type",
        "provider_request_id", "strapi_url", "provider",
        "strapi_media_id", "strapi_media_document_id", "created_at",
        "prompt_name", "prompt_version", "prompt_hash",
    ):
        assert name in columns, f"missing column {name}"
    for nullable_name in (
        "section_heading", "insertion_marker", "local_path",
        "mime_type", "provider_request_id", "strapi_url",
        "strapi_media_id", "strapi_media_document_id",
        "prompt_name", "prompt_version", "prompt_hash",
    ):
        assert columns[nullable_name]["nullable"] is True, nullable_name
    # job_id is indexed for per-job lookups.
    indexes = {i["name"]: i for i in inspect(engine).get_indexes("images")}
    job_index = next(
        (i for i in indexes.values() if "job_id" in i.get("column_names", [])),
        None,
    )
    assert job_index is not None, "no index covering images.job_id"


async def test_image_pipeline_full_run(env, tmp_path):
    """Planner (fake LLM) + generation (fake provider) against PG."""
    settings = env["settings"]
    with SessionLocal() as session:
        job = session.get(GenerationJob, env["job_id"])
        llm = FakeLLM([json.dumps(PLAN_THREE)], settings)
        plan, rows = await run_image_planner(session, job, llm)
        await llm.aclose()

        assert plan.total_count == 3
        assert [r.role for r in rows] == ["hero", "inline", "inline"]
        assert job.status == JobStatus.IMAGE_GENERATING.value

        provider = FakeImageProvider(settings)
        rows = await run_image_generation(session, job, provider,
                                          settings=settings)

        # H05: the step no longer marks READY (the orchestrator sets it in
        # the same transaction as the DoD gate); the job stays
        # image_generating when the step is driven directly.
        assert job.status == JobStatus.IMAGE_GENERATING.value
        job_id = job.id
        filenames = [r.filename for r in rows]

    # Section 34 layout on disk.
    img_dir = tmp_path / "articles" / str(job_id) / "images"
    for name in ("hero.webp", "inline-1.webp", "inline-2.webp"):
        assert (img_dir / name).exists(), name
    # L01: the research JSONs are exported next to the article too.
    job_dir = tmp_path / "articles" / str(job_id)
    for name in ("content-brief.json", "outline.json", "serp.json",
                 "review.json", "sources.json"):
        assert (job_dir / name).exists(), f"missing {name}"
    assert filenames == ["hero.webp", "inline-1.webp", "inline-2.webp"]

    article_md = (tmp_path / "articles" / str(job_id) / "article.md").read_text()
    assert "![Inline one alt](images/inline-1.webp)" in article_md
    assert "![Inline two alt](images/inline-2.webp)" in article_md
    assert "[[IMAGE:" not in article_md
    assert "images/hero.webp" not in article_md  # default flag: hero out

    article_json = (tmp_path / "articles" / str(job_id) / "article.json")
    payload = json.loads(article_json.read_text())
    assert {i["filename"] for i in payload["images"]} == set(filenames)

    # Rows in PG: provenance filled, strapi_* left NULL until P7.
    with SessionLocal() as session:
        rows = session.scalars(
            select(ImageRow).where(ImageRow.job_id == job_id)
        ).all()
        assert len(rows) == 3
        for row in rows:
            assert row.local_path.endswith(row.filename)
            # M07: .webp filenames hold real WebP bytes (transcoded).
            assert row.mime_type == "image/webp"
            assert row.provider == "fake-image-model"
            assert row.provider_request_id
            assert row.strapi_url is None
            assert row.strapi_media_id is None
            assert row.strapi_media_document_id is None


async def test_planner_rerun_replaces_rows(env):
    """Re-running the planner swaps the plan: no orphan rows."""
    settings = env["settings"]
    with SessionLocal() as session:
        job = session.get(GenerationJob, env["job_id"])
        llm = FakeLLM(
            [json.dumps(PLAN_THREE), json.dumps(PLAN_HERO_ONLY)], settings
        )
        await run_image_planner(session, job, llm)
        plan, rows = await run_image_planner(session, job, llm)
        await llm.aclose()
        job_id = job.id
        assert plan.total_count == 1
        remaining = session.scalars(
            select(ImageRow).where(ImageRow.job_id == job_id)
        ).all()
        assert len(remaining) == 1
        assert remaining[0].role == "hero"

    # FK cascade sanity: the images table is real (query via SQL).
    with SessionLocal() as session:
        n = session.execute(
            text(
                "SELECT count(*) FROM images WHERE job_id = :jid"
            ),
            {"jid": str(job_id)},
        ).scalar()
        assert n == 1
