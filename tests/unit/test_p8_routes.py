"""P8 unit: web UI + REST routes (spec sections 43, 44, 45).

Drives the full FastAPI app with a SQLite database override and no real
Redis / LLM / Strapi. Enqueueing is monkeypatched so job creation and the
on-demand Strapi push are verified without touching the RQ queue. Provider
status is driven by fakes (section 60: reachability booleans only).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models  # noqa: F401 - register all models on Base
from app.db.base import Base
from app.db.models.job import GenerationJob
from app.db.session import get_db
from app.main import create_app
from app.pipeline.orchestrator import PipelineProviders


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

    # Record enqueues without touching Redis.
    enqueued_jobs: list[str] = []
    enqueued_syncs: list[str] = []

    def fake_enqueue_job(job_id):
        enqueued_jobs.append(str(job_id))

    def fake_enqueue_strapi_sync(job_id):
        enqueued_syncs.append(str(job_id))

    monkeypatch.setattr(
        "app.workers.article_tasks.enqueue_job", fake_enqueue_job
    )
    monkeypatch.setattr(
        "app.workers.article_tasks.enqueue_strapi_sync", fake_enqueue_strapi_sync
    )
    # Provider status must not hit the network: all providers report down.
    monkeypatch.setattr(
        "app.routes.providers.build_providers",
        lambda settings=None: PipelineProviders(),
    )

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as test_client:
        test_client.enqueued_jobs = enqueued_jobs
        test_client.enqueued_syncs = enqueued_syncs
        test_client.db_session = lambda: test_session()
        yield test_client


def _new_job_payload(keyword="p8unit how to bake sourdough", **overrides):
    payload = {
        "keyword": keyword,
        "language": "en",
        "market": "US",
        "strategy": "auto",
        "image_count_override": None,
    }
    payload.update(overrides)
    return payload


# ----------------------------------------------------------------------
# REST: job collection
# ----------------------------------------------------------------------
def test_api_create_job_returns_id_and_queued(client):
    response = client.post("/api/jobs", json=_new_job_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    # The id is a valid UUID and the run was enqueued to RQ.
    parsed = uuid.UUID(body["job_id"])
    assert str(parsed) == body["job_id"]
    assert client.enqueued_jobs == [body["job_id"]]


def test_api_create_job_persists_clamped_image_override(client):
    response = client.post(
        "/api/jobs", json=_new_job_payload(image_count_override=2)
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    with client.db_session() as session:
        job = session.get(GenerationJob, uuid.UUID(job_id))
    assert job.image_count_override == 2


def test_m14_enqueue_failure_degrades_and_recovers(client, monkeypatch):
    """M14 / spec 43.3: an RQ enqueue failure (Redis down) is NOT shown as a
    normal queued job. It persists an explicit ``ENQUEUE_FAILED`` marker, the
    API reports ``enqueued=False`` + ``error``, and a safe re-enqueue after
    Redis recovers does NOT create a new job row."""
    from app.routes import jobs as jobs_routes

    # ---- Redis DOWN: enqueue raises -> _enqueue returns False ----
    monkeypatch.setattr(
        jobs_routes, "_enqueue", lambda job_id, options=None: False
    )
    resp = client.post("/api/jobs", json=_new_job_payload(keyword="m14 redis down"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"          # pipeline never started -> queued
    assert body["enqueued"] is False
    assert body["error"]
    job_id = body["job_id"]

    # The marker is persisted on the job row.
    with client.db_session() as session:
        job = session.get(GenerationJob, uuid.UUID(job_id))
        assert job.status == "queued"
        assert job.error_code == "ENQUEUE_FAILED"

    # The job list still shows it (count = 1 so far).
    assert len(client.get("/api/jobs").json()["jobs"]) == 1

    # While ENQUEUE_FAILED, the fragment offers the safe re-enqueue button
    # (not a terminal retry) so the operator can recover after Redis is up.
    frag = client.get(f"/jobs/{job_id}/fragment")
    assert "/api/jobs/%s/enqueue" % job_id in frag.text
    assert "入队失败" in frag.text

    # ---- Redis UP: safe re-enqueue recovers the SAME job, no new row ----
    monkeypatch.setattr(jobs_routes, "_enqueue", lambda job_id, options=None: True)
    re = client.post(f"/api/jobs/{job_id}/enqueue")
    assert re.status_code == 200
    re_body = re.json()
    assert re_body["enqueued"] is True
    assert re_body["status"] == "queued"

    # Error marker cleared; SAME job id; still exactly ONE job row.
    with client.db_session() as session:
        job = session.get(GenerationJob, uuid.UUID(job_id))
        assert job.error_code is None
        assert job.error_message is None
        assert job.status == "queued"
    assert len(client.get("/api/jobs").json()["jobs"]) == 1


def test_m14_re_enqueue_rejects_running_job(client, monkeypatch):
    """M14: re-enqueue is rejected once the job has left ``queued`` (would
    double-run the pipeline)."""
    job_id = client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
    with client.db_session() as session:
        job = session.get(GenerationJob, uuid.UUID(job_id))
        job.status = "serp_searching"  # the pipeline has started
        session.commit()
    resp = client.post(f"/api/jobs/{job_id}/enqueue")
    assert resp.status_code == 409


def test_api_create_job_rejects_out_of_range_image_override(client):
    # The schema enforces 1..3; 4 is rejected with 422.
    response = client.post(
        "/api/jobs", json=_new_job_payload(image_count_override=4)
    )
    assert response.status_code == 422


def test_api_create_job_rejects_blank_keyword(client):
    response = client.post("/api/jobs", json=_new_job_payload(keyword="   "))
    assert response.status_code == 422


def test_api_create_job_rejects_unknown_strategy(client):
    response = client.post("/api/jobs", json=_new_job_payload(strategy="bogus"))
    assert response.status_code == 422


def test_api_list_jobs(client):
    client.post("/api/jobs", json=_new_job_payload("p8unit list one"))
    client.post("/api/jobs", json=_new_job_payload("p8unit list two"))
    response = client.get("/api/jobs")
    assert response.status_code == 200
    keywords = {row["keyword"] for row in response.json()["jobs"]}
    assert "p8unit list one" in keywords
    assert "p8unit list two" in keywords


# ----------------------------------------------------------------------
# REST: single job + lifecycle
# ----------------------------------------------------------------------
def test_api_get_job_and_404(client):
    job_id = client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
    response = client.get(f"/api/jobs/{job_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["progress"]["total"] == 15
    assert client.get(f"/api/jobs/{uuid.uuid4()}").status_code == 404


def _set_status(client, job_id, status):
    with client.db_session() as session:
        job = session.get(GenerationJob, uuid.UUID(job_id))
        job.status = status
        if status == "failed":
            job.error_code = "LLM_UNAVAILABLE"
            job.error_message = "no llm"
        session.commit()


def test_api_retry_terminal_job(client):
    job_id = client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
    _set_status(client, job_id, "failed")
    response = client.post(f"/api/jobs/{job_id}/retry")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["enqueued"] is True
    assert client.enqueued_jobs[-1] == job_id


def test_api_retry_non_terminal_job_409(client):
    job_id = client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 409


def test_api_cancel_non_terminal_job(client):
    job_id = client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
    response = client.post(f"/api/jobs/{job_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_api_cancel_terminal_job_409(client):
    job_id = client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
    _set_status(client, job_id, "ready")
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 409


def test_api_job_article_empty(client):
    job_id = client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
    response = client.get(f"/api/jobs/{job_id}/article")
    assert response.status_code == 200
    assert response.json()["article"] is None


def test_api_job_serp_and_sources_empty(client):
    job_id = client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
    assert client.get(f"/api/jobs/{job_id}/serp").json()["serp"] is None
    assert client.get(f"/api/jobs/{job_id}/sources").json()["sources"] == []
    assert client.get(f"/api/jobs/{job_id}/reviews").json()["reviews"] == []


def test_api_sync_strapi_enqueues(client):
    job_id = client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
    _set_status(client, job_id, "ready")
    response = client.post(f"/api/jobs/{job_id}/sync-strapi")
    assert response.status_code == 200
    assert response.json()["enqueued"] is True
    assert client.enqueued_syncs == [job_id]


def test_api_sync_strapi_failed_job_409(client):
    """H05: a failed pipeline job's body failed the DoD gate — the push
    endpoint must refuse it (409), not enqueue a CMS write."""
    job_id = client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
    _set_status(client, job_id, "failed")
    response = client.post(f"/api/jobs/{job_id}/sync-strapi")
    assert response.status_code == 409
    assert client.enqueued_syncs == []


def test_api_sync_strapi_cancelled_job_409(client):
    job_id = client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
    _set_status(client, job_id, "cancelled")
    assert client.post(f"/api/jobs/{job_id}/sync-strapi").status_code == 409
    assert client.enqueued_syncs == []


def test_api_sync_strapi_non_terminal_status_409(client):
    """H05: mid-pipeline (non-terminal) statuses are not syncable either."""
    job_id = client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
    # created job is 'queued'
    assert client.post(f"/api/jobs/{job_id}/sync-strapi").status_code == 409
    assert client.enqueued_syncs == []


def test_api_sync_strapi_draft_created_repush(client):
    """H05: an already-synced draft may be re-pushed (the update path)."""
    job_id = client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
    _set_status(client, job_id, "strapi_draft_created")
    response = client.post(f"/api/jobs/{job_id}/sync-strapi")
    assert response.status_code == 200
    assert client.enqueued_syncs == [job_id]


def test_api_sync_strapi_sync_failed_retry_200(client):
    """M01: a persisted strapi sync failure (section 64) is syncable —
    the dedicated retry path enqueues an UPDATE of the same draft."""
    job_id = client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
    _set_status(client, job_id, "strapi_sync_failed")
    response = client.post(f"/api/jobs/{job_id}/sync-strapi")
    assert response.status_code == 200
    assert response.json()["enqueued"] is True
    assert client.enqueued_syncs == [job_id]


def test_api_retry_refuses_sync_failed(client):
    """M01: strapi_sync_failed is NOT pipeline-terminal — the full/step
    retry endpoint stays 409 (the article pipeline already succeeded;
    only the dedicated sync retry applies). Cancel is still allowed:
    the user may abandon a sync-failed job."""
    job_id = client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
    _set_status(client, job_id, "strapi_sync_failed")
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 409
    response = client.post(f"/api/jobs/{job_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


# ----------------------------------------------------------------------
# REST: datasets / strapi / providers
# ----------------------------------------------------------------------
def test_api_datasets_keywords_empty(client):
    response = client.get("/api/datasets/keywords?strategy=low_kd")
    assert response.status_code == 200
    assert response.json()["count"] == 0


def test_api_datasets_strategy_filter(client):
    from app.db.models.keyword import Keyword, KeywordCluster

    with client.db_session() as session:
        cluster = KeywordCluster(name="p8unit cluster", sheet_name="p8unit sheet")
        session.add(cluster)
        session.flush()
        session.add(
            Keyword(
                cluster_id=cluster.id,
                keyword="p8unit high volume kw",
                volume=5000,
                source="excel",
            )
        )
        session.commit()
    response = client.get("/api/datasets/keywords?strategy=high_volume")
    assert response.status_code == 200
    keywords = [row["keyword"] for row in response.json()["keywords"]]
    assert "p8unit high volume kw" in keywords


def test_api_datasets_keyword_import(client, tmp_path):
    from openpyxl import Workbook

    path = tmp_path / "p8unit_import.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "P8Cluster"
    ws.append(["Keyword", "Search Volume", "Keyword Difficulty", "CPC", "Intent"])
    ws.append(["p8unit imported kw", 3000, 12.5, 1.5, "Informational"])
    ws.append(["p8unit second kw", 800, 40, 0.7, "Transactional"])
    wb.save(str(path))

    with open(path, "rb") as handle:
        response = client.post(
            "/api/datasets/keywords/import",
            files={"file": ("p8unit_import.xlsx", handle, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["keywords_created"] == 2
    assert body["clusters_created"] == 1
    # The imported rows are now queryable through the dataset endpoint.
    rows = client.get("/api/datasets/keywords").json()["keywords"]
    assert any(r["keyword"] == "p8unit imported kw" for r in rows)


def test_api_datasets_import_rejects_non_xlsx(client, tmp_path):
    path = tmp_path / "p8unit_bad.txt"
    path.write_text("not a spreadsheet")
    with open(path, "rb") as handle:
        response = client.post(
            "/api/datasets/keywords/import",
            files={"file": ("p8unit_bad.txt", handle, "text/plain")},
        )
    assert response.status_code == 400


def test_api_strapi_not_configured(client):
    # .env has an empty STRAPI_API_TOKEN → not_configured, no network.
    response = client.get("/api/strapi/authors")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["reason"] == "not_configured"
    assert client.get("/api/strapi/categories").json()["items"] == []


def test_api_provider_status_reachability_only(client):
    # build_providers is faked to all-None → every provider is reachable: False.
    response = client.get("/api/providers/status")
    assert response.status_code == 200
    body = response.json()
    names = set(body["providers"].keys())
    assert names == {"llm", "serp", "extractor", "image", "cms"}
    for value in body["providers"].values():
        assert value == {"reachable": False}


# ----------------------------------------------------------------------
# Web pages
# ----------------------------------------------------------------------
def test_web_new_article_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "New Article" in response.text
    assert "Target Keyword" in response.text
    assert "htmx.min.js" in response.text


def test_web_jobs_list_page(client):
    client.post("/api/jobs", json=_new_job_payload("p8unit web list"))
    response = client.get("/jobs")
    assert response.status_code == 200
    assert "p8unit web list" in response.text


def test_web_generate_creates_job_and_redirects(client):
    response = client.post(
        "/generate",
        data={
            "keyword": "p8unit web generate",
            "language": "en",
            "market": "US",
            "target_function": "",
            "strategy": "auto",
            "author_document_id": "",
            "category_document_id": "",
            "image_mode": "auto",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/jobs/")
    # The run was enqueued; a new job row exists.
    assert len(client.enqueued_jobs) == 1
    assert client.enqueued_jobs[0] in location


def test_web_generate_rejects_bad_image_mode(client):
    response = client.post(
        "/generate",
        data={
            "keyword": "p8unit bad image",
            "language": "en",
            "market": "US",
            "target_function": "",
            "strategy": "auto",
            "author_document_id": "",
            "category_document_id": "",
            "image_mode": "4",
        },
    )
    assert response.status_code == 400


def test_web_job_detail_and_fragment(client):
    job_id = client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
    detail = client.get(f"/jobs/{job_id}")
    assert detail.status_code == 200
    assert "Pipeline" in detail.text
    assert "Content Brief" in detail.text
    fragment = client.get(f"/jobs/{job_id}/fragment")
    assert fragment.status_code == 200
    assert "detail-body" in fragment.text


def test_web_job_detail_research_and_logs(client):
    """M13: the job detail exposes the P4 research body (competitor analyses,
    SERP synthesis, evidence notes) and a Logs block; the fragment renders
    them read-only (spec 43.3)."""
    from app.db.models.research import (
        CompetitorAnalysisRow,
        EvidenceNoteRow,
        SerpSynthesisRow,
    )
    from app.db.models.source import SourcePage

    job_id = client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
    with client.db_session() as session:
        job = session.get(GenerationJob, uuid.UUID(job_id))
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        page = SourcePage(
            url="https://competitor.example/a",
            normalized_url="https://competitor.example/a",
            url_hash="a" * 64,
            title="Competitor A",
            domain="competitor.example",
            content_markdown="competitor body",
            content_hash="b" * 64,
            extractor="mock",
            first_seen_at=now,
            last_fetched_at=now,
        )
        session.add(page)
        session.flush()
        session.add(CompetitorAnalysisRow(
            job_id=job.id, source_page_id=page.id,
            analysis={"strengths": ["x"], "gaps": ["y"]}, model="m1",
        ))
        session.add(SerpSynthesisRow(
            job_id=job.id, synthesis={"theme": "how to bake sourdough"},
        ))
        session.add(EvidenceNoteRow(
            job_id=job.id, claim="80% hydration is typical",
            source_title="Competitor A", source_url="https://competitor.example/a",
            source_type="serp", confidence="high", usage="used", note="n",
        ))
        session.commit()

    # job_detail_payload (drives /jobs/{id} and the fragment) carries the
    # three research sections + a Logs block (M13).
    from app.routes.jobs import job_detail_payload

    with client.db_session() as session:
        job = session.get(GenerationJob, uuid.UUID(job_id))
        body = job_detail_payload(session, job)
    assert len(body["competitor_analyses"]) == 1
    assert body["competitor_analyses"][0]["source_title"] == "Competitor A"
    assert body["competitor_analyses"][0]["analysis"]["strengths"] == ["x"]
    assert body["synthesis"] == {"theme": "how to bake sourdough"}
    assert body["evidence_notes"][0]["claim"] == "80% hydration is typical"
    assert body["evidence_notes"][0]["confidence"] == "high"
    assert isinstance(body["logs"]["steps"], list)
    assert body["logs"]["steps"][0]["label"]

    # The fragment renders them read-only.
    fragment = client.get(f"/jobs/{job_id}/fragment")
    html = fragment.text
    assert "Competitor Analyses" in html
    assert "SERP Synthesis" in html
    assert "Evidence Notes" in html
    assert "Logs" in html
    assert "how to bake sourdough" in html  # synthesis content
    assert "80% hydration is typical" in html  # evidence claim


def test_web_article_preview_empty(client):
    job_id = client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
    response = client.get(f"/articles/{job_id}")
    assert response.status_code == 200
    assert "Article Preview" in response.text


# ------------------------------------------------------------ M03 (rendered preview)
def _preview_session(client):
    """A session on the same in-memory engine the TestClient's requests use."""
    from app.db.session import get_db as _get_db

    dep = client.app.dependency_overrides[_get_db]
    gen = dep()  # a generator that yields a session
    return next(gen)


def test_web_article_preview_renders_markdown(client, tmp_path):
    """M03: the preview shows RENDERED HTML (headings/tables), not the raw
    Markdown source, and a hero image with its alt text."""
    from app.db.models.article import ArticleVersionRow
    from app.db.models.images import ImageRow

    job_id = client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
    session = _preview_session(client)
    body = (
        "## Introduction\n\n"
        "| Col A | Col B |\n"
        "|-------|-------|\n"
        "| 1 | 2 |\n\n"
        "More text.\n"
    )
    session.add(ArticleVersionRow(
        job_id=uuid.UUID(job_id), version=2, stage="revision", title="My Title",
        body_markdown=body, seo_title="SEO T", meta_description="MD", slug="my-slug",
    ))
    # A hero image file so the hero <img> renders with its alt text.
    # (_local_image_url only checks the file exists; the static route is not
    # exercised here, so no DATA_DIR override is needed.)
    hero_file = tmp_path / "articles" / job_id / "images" / "hero.webp"
    hero_file.parent.mkdir(parents=True)
    hero_file.write_bytes(b"\xff\xd8x")
    session.add(ImageRow(
        job_id=uuid.UUID(job_id), role="hero", sort_order=1, purpose="hero",
        prompt="p", filename="hero.webp", alt_text="Hero caption",
        aspect_ratio="16:9", provider="openai", local_path=str(hero_file),
    ))
    session.commit()

    resp = client.get(f"/articles/{job_id}")
    assert resp.status_code == 200
    html = resp.text
    # Rendered: an <h2> heading and a real <table>, NOT the raw markdown.
    assert "<h2" in html
    assert "<table" in html
    assert "|-------|" not in html  # the raw table separator is gone
    # Hero image with its alt text.
    assert 'alt="Hero caption"' in html
    session.close()


def test_web_article_preview_escapes_malicious_html(client):
    """M03: raw HTML in the body is ESCAPED (section 60) — a <script> tag in
    the article body must appear as literal text, never as executable HTML."""
    from app.db.models.article import ArticleVersionRow

    job_id = client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
    session = _preview_session(client)
    body = "## Head\n\n<script>alert('pwned')</script>\n\nok\n"
    session.add(ArticleVersionRow(
        job_id=uuid.UUID(job_id), version=1, stage="writer", title="T",
        body_markdown=body, seo_title="s", meta_description="m", slug="s",
    ))
    session.commit()

    resp = client.get(f"/articles/{job_id}")
    assert resp.status_code == 200
    # The script tag must be escaped, not present as live HTML.
    assert "<script>alert('pwned')</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text
    session.close()


def test_web_article_preview_inline_image_marker(client, tmp_path):
    """M03: an inline image marker is resolved to a local <img> with its alt."""
    from app.db.models.article import ArticleVersionRow
    from app.db.models.images import ImageRow

    job_id = client.post("/api/jobs", json=_new_job_payload()).json()["job_id"]
    session = _preview_session(client)
    body = "## Section\n\nSome paragraph.\n\n[[IMAGE:inline-1]]\n"
    session.add(ArticleVersionRow(
        job_id=uuid.UUID(job_id), version=1, stage="writer", title="T",
        body_markdown=body, seo_title="s", meta_description="m", slug="s",
    ))
    img_file = tmp_path / "articles" / job_id / "images" / "inline-1.webp"
    img_file.parent.mkdir(parents=True)
    img_file.write_bytes(b"\xff\xd8y")
    session.add(ImageRow(
        job_id=uuid.UUID(job_id), role="inline", sort_order=1, purpose="inline",
        prompt="p", filename="inline-1.webp", alt_text="Inline caption",
        aspect_ratio="16:9", provider="openai",
        section_heading="Section", insertion_marker="inline-1",
        local_path=str(img_file),
    ))
    session.commit()

    resp = client.get(f"/articles/{job_id}")
    assert resp.status_code == 200
    html = resp.text
    assert "<img" in html
    assert 'alt="Inline caption"' in html
    # The raw marker is resolved away.
    assert "[[IMAGE:inline-1]]" not in html
    session.close()


def test_web_keywords_page(client):
    response = client.get("/keywords")
    assert response.status_code == 200
    assert "Keyword Dataset" in response.text
    assert "Import" in response.text


def test_web_settings_page_statuses_only(client, monkeypatch):
    """M-1 / spec 57: /settings renders the seven health rows and shows
    only the four allowed status words — never any secret value."""
    # The web route imports build_providers into its own namespace; patch
    # both so no real provider (and thus no network) is constructed.
    monkeypatch.setattr(
        "app.routes.web.build_providers",
        lambda settings=None: PipelineProviders(),
    )
    monkeypatch.setattr("app.routes.web.check_database", lambda: True)
    monkeypatch.setattr("app.routes.web.check_redis", lambda url: True)

    response = client.get("/settings")
    assert response.status_code == 200
    # All seven rows are present (spec 57).
    for name in (
        "LLM",
        "DataForSEO",
        "Exa",
        "Image API",
        "Strapi",
        "PostgreSQL",
        "Redis",
    ):
        assert name in response.text
    # The only status words rendered are the four allowed ones.
    import re

    statuses = re.findall(
        r'class="badge status-(connected|configured|missing|failed)"',
        response.text,
    )
    assert len(statuses) == 7
    # The LLM is configured in the test .env (llm_base_url set) and its
    # fake reports down → Failed; DataForSEO/Exa/Image/Strapi are
    # unconfigured → Missing; DB and Redis are patched reachable.
    assert statuses.count("missing") == 4
    assert statuses.count("connected") == 2
    assert statuses.count("failed") == 1
