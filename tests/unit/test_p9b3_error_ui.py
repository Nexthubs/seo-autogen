"""P9-B3 unit: better error UI (spec P9 "Better error UI").

Covers:
* ``error_catalog`` — every spec-section-52 ``ErrorCode`` has a dedicated
  Chinese cause + advice entry; unknown / provider-derived codes
  (``HTTP_*``, ``UNEXPECTED``, ``NO_PAGE``) fall back gracefully.
* ``error`` payload (REST ``GET /api/jobs/{id}`` + detail page) —
  shape, and ``last_failed_step`` derived at read time from checkpoint
  rows (first incomplete step), with the human label.
* The job-detail HTMX fragment — Chinese error panel + the one-click
  "retry from that step" form wired to the P9-A ``retry_step`` endpoint.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.exceptions import ErrorCode
from app.db import models  # noqa: F401 - register all models on Base
from app.db.base import Base
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
from app.db.models.serp import SerpRun
from app.db.models.source import JobSource, SourcePage
from app.db.session import get_db
from app.main import create_app
from app.services.error_catalog import explain_error

# Steps 1..15 checkpoint rows needed to mark each step "done"
# (checkpoints.step_done predicates).
ALL_STEPS = 15


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        db = test_session()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr("app.workers.article_tasks.enqueue_job", lambda job_id: None)
    monkeypatch.setattr(
        "app.workers.article_tasks.enqueue_strapi_sync", lambda job_id: None
    )
    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as test_client:
        test_client.db_session = lambda: test_session()
        yield test_client


def _failed_job(session, *, keyword, error_code="LLM_STRUCTURED_OUTPUT_INVALID"):
    job = GenerationJob(
        keyword=keyword,
        status="failed",
        current_step="failed",
        error_code=error_code,
        error_message="structured output failed validation (test)",
        started_at=datetime.datetime.now(datetime.timezone.utc),
    )
    session.add(job)
    session.flush()
    return job


def _source_page(session) -> SourcePage:
    now = datetime.datetime.now(datetime.timezone.utc)
    page = SourcePage(
        url="https://example.com/p9b3",
        normalized_url="https://example.com/p9b3",
        url_hash="a" * 64,
        domain="example.com",
        content_markdown="x",
        content_hash="b" * 64,
        extractor="fake",
        first_seen_at=now,
        last_fetched_at=now,
    )
    session.add(page)
    session.flush()
    return page


def _writer_version(session, job: GenerationJob) -> ArticleVersionRow:
    av = ArticleVersionRow(
        job_id=job.id, version=1, stage="writer", title="t",
        body_markdown="b", seo_title="s", meta_description="m", slug="p9b3",
    )
    session.add(av)
    session.flush()
    return av


def _writer_version_id(session, job: GenerationJob):
    return (
        session.scalars(
            select(ArticleVersionRow).where(
                ArticleVersionRow.job_id == job.id,
                ArticleVersionRow.stage == "writer",
            )
        )
        .first()
        .id
    )


def _job_source_page(session, job: GenerationJob) -> SourcePage:
    return (
        session.scalars(
            select(SourcePage)
            .join(JobSource, JobSource.source_page_id == SourcePage.id)
            .where(JobSource.job_id == job.id)
        )
        .first()
    )


def seed_checkpoints_through(session, job: GenerationJob, upto: int) -> None:
    """Commit checkpoint rows for steps 1..``upto`` (1-based).

    Step 1 (``keyword_prepare``) is already "done": ``_failed_job`` sets
    ``started_at``, which is its done-signal.
    """
    if upto >= 2:
        session.add(SerpRun(
            job_id=job.id, provider="dataforseo", query=job.keyword,
            location_code="us", language_code="en", device="desktop",
            raw_response={},
        ))
    if upto >= 3:
        page = _source_page(session)
        session.add(JobSource(job_id=job.id, source_page_id=page.id))
    if upto >= 4:
        session.add(CompetitorAnalysisRow(
            job_id=job.id, source_page_id=_job_source_page(session, job).id,
            analysis={},
        ))
    if upto >= 5:
        session.add(SerpSynthesisRow(job_id=job.id, synthesis={}))
    if upto >= 6:
        session.add(EvidenceNoteRow(
            job_id=job.id, claim="c", source_title="t",
            source_url="https://example.com", source_type="web",
            confidence=0.9, usage="supporting",
        ))
    if upto >= 7:
        session.add(ContentBriefRow(job_id=job.id, brief={}))
    if upto >= 8:
        session.add(ArticleOutlineRow(job_id=job.id, outline={}))
    if upto >= 9:
        _writer_version(session, job)
    if upto >= 10:
        session.add(ArticleReviewRow(
            job_id=job.id, article_version_id=_writer_version_id(session, job),
            review_type="seo", review={},
        ))
    if upto >= 11:
        session.add(ArticleReviewRow(
            job_id=job.id, article_version_id=_writer_version_id(session, job),
            review_type="fact", review={},
        ))
    if upto >= 12:
        session.add(ArticleReviewRow(
            job_id=job.id, article_version_id=_writer_version_id(session, job),
            review_type="style", review={},
        ))
    if upto >= 13:
        session.flush()
        reviews = session.scalars(
            select(ArticleReviewRow).where(
                ArticleReviewRow.job_id == job.id,
                ArticleReviewRow.review_type.in_(("seo", "fact", "style")),
            )
        ).all()
        session.add(ArticleVersionRow(
            job_id=job.id, version=2, stage="revision", title="t2",
            body_markdown="b2", seo_title="s2", meta_description="m2",
            slug="p9b3-v2",
            based_on_reviews={
                row.review_type: {
                    "review_id": str(row.id),
                    "attempt": row.attempt,
                }
                for row in reviews
            },
        ))
    if upto >= 14:
        session.add(ImageRow(
            job_id=job.id, role="hero", sort_order=0, purpose="p",
            prompt="pr", filename="hero.png", alt_text="a",
            aspect_ratio="16:9", provider="fake",
        ))
    if upto >= 15:
        row = session.scalars(
            select(ImageRow).where(ImageRow.job_id == job.id)
        ).first()
        row.local_path = "/tmp/hero.png"


# ------------------------------------------------------ error_catalog
def test_catalog_covers_every_spec_error_code():
    """Every section-52 ErrorCode has a dedicated (non-fallback) entry."""
    for member in ErrorCode:
        payload = explain_error(member.value, "some message")
        assert payload["code"] == member.value
        assert payload["message"] == "some message"
        for field in ("category", "reason", "advice"):
            assert payload[field] and payload[field].strip()
        # A dedicated entry is anything other than the generic fallback.
        assert payload["category"] != "未知错误"


def test_catalog_last_failed_step_fields_default_none():
    payload = explain_error("EXTRACTOR_FAILED", "boom")
    assert payload["last_failed_step"] is None
    assert payload["last_failed_step_label"] is None
    payload = explain_error(
        "EXTRACTOR_FAILED", "boom",
        last_failed_step=3, step_label="Source extraction",
    )
    assert payload["last_failed_step"] == 3
    assert payload["last_failed_step_label"] == "Source extraction"


def test_unknown_and_derived_codes_fall_back():
    http = explain_error("HTTP_429", "rate limited")
    assert "429" in http["category"]
    unexpected = explain_error("UNEXPECTED", "kaboom")
    assert unexpected["category"] == "未知错误"
    none = explain_error(None, "trace")
    assert none["category"] == "未知错误"
    no_page = explain_error("NO_PAGE", "page missing")
    assert no_page["category"] == "页面无法抓取"


# ------------------------------------------------- REST /api/jobs/{id}
def test_api_job_error_payload_derives_first_incomplete_step(client: TestClient):
    # No checkpoint rows at all. Step 1 (keyword_prepare) is "done" the
    # moment the run starts (its done-signal is ``started_at``, which a
    # failed job always has), so the derived failure step is 2 (serp_search).
    with client.db_session() as session:
        job = _failed_job(session, keyword="p9b3 no checkpoints")
        session.commit()
        job_id = job.id

    payload = client.get(f"/api/jobs/{job_id}").json()
    assert payload["error"] == {
        "code": "LLM_STRUCTURED_OUTPUT_INVALID",
        "message": "structured output failed validation (test)",
        "category": "LLM 结构化输出校验失败",
        "reason": payload["error"]["reason"],
        "advice": payload["error"]["advice"],
        "last_failed_step": 2,
        "last_failed_step_label": "SERP search",
        # Audit M11: raw failure detail is exposed for debugging.
        "error_raw": None,
    }
    # Exact key set (code/message stay for the machine-readable contract).
    assert set(payload["error"]) == {
        "code", "message", "category", "reason", "advice",
        "last_failed_step", "last_failed_step_label", "error_raw",
    }


def test_api_job_error_payload_after_partial_checkpoints(client: TestClient):
    """Steps 1..8 checkpointed → last_failed_step is 9 (article_writer)."""
    with client.db_session() as session:
        job = _failed_job(session, keyword="p9b3 partial")
        seed_checkpoints_through(session, job, 8)
        session.commit()
        job_id = job.id

    error = client.get(f"/api/jobs/{job_id}").json()["error"]
    assert error["last_failed_step"] == 9
    assert error["last_failed_step_label"] == "Article draft"


def test_api_job_error_payload_all_steps_done(client: TestClient):
    """All 15 checkpoints present (post-pipeline failure) → step is None."""
    with client.db_session() as session:
        job = _failed_job(
            session, keyword="p9b3 all done", error_code="STRAPI_SLUG_CONFLICT",
        )
        seed_checkpoints_through(session, job, ALL_STEPS)
        session.commit()
        job_id = job.id

    error = client.get(f"/api/jobs/{job_id}").json()["error"]
    assert error["code"] == "STRAPI_SLUG_CONFLICT"
    assert error["category"] == "Strapi Slug 冲突"
    assert error["last_failed_step"] is None
    assert error["last_failed_step_label"] is None


def test_api_job_no_error_when_not_failed(client: TestClient):
    with client.db_session() as session:
        job = GenerationJob(keyword="p9b3 ready", status="ready")
        session.add(job)
        session.commit()
        job_id = job.id
    assert client.get(f"/api/jobs/{job_id}").json()["error"] is None


# -------------------------------------------------- job-detail fragment
def test_fragment_renders_error_panel_with_retry_from_step(client: TestClient):
    with client.db_session() as session:
        job = _failed_job(session, keyword="p9b3 frag")
        seed_checkpoints_through(session, job, 5)  # fail at step 6
        session.commit()
        job_id = job.id

    resp = client.get(f"/jobs/{job_id}/fragment")
    assert resp.status_code == 200
    body = resp.text
    # Chinese cause + advice lines.
    assert "原因" in body and "说明" in body and "处置建议" in body
    assert "LLM 结构化输出校验失败" in body
    # One-click retry of the derived step (P9-A retry_step endpoint).
    assert "从该步重试" in body
    assert 'name="step" value="6"' in body
    assert "Evidence research" in body  # step 6 label
    # The generic retry-card select still exists too.
    assert "Retry (terminal job)" in body


def test_fragment_error_panel_no_step_for_post_pipeline_failure(
    client: TestClient,
):
    with client.db_session() as session:
        job = _failed_job(
            session, keyword="p9b3 frag all", error_code="STRAPI_SLUG_CONFLICT",
        )
        seed_checkpoints_through(session, job, ALL_STEPS)
        session.commit()
        job_id = job.id

    body = client.get(f"/jobs/{job_id}/fragment").text
    assert "失败" in body
    assert "Strapi Slug 冲突" in body
    # No per-step retry offered — every step already checkpointed.
    assert "从该步重试" not in body
    assert "Strapi 同步阶段" in body
