"""P9-B2 unit: prompt version dashboard (spec sections 47, 48).

Covers:
* ``prompt_service.all_prompt_specs`` — the 12 versioned prompts, the two
  unversioned rulebooks excluded, SHA256 hashes are 64-char hex.
* ``GET /api/prompts`` — inventory + usage payload shape.
* ``GET /prompts`` — the dashboard page renders.
* Usage aggregation over the provenance tables, incl. a hash-drift (stale
  version) case.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models  # noqa: F401 - register all models on Base
from app.db.base import Base
from app.db.models.article import ArticleReviewRow, ArticleVersionRow
from app.db.models.images import ImageRow
from app.db.models.job import GenerationJob
from app.db.models.research import CompetitorAnalysisRow
from app.db.models.source import SourcePage
from app.db.session import get_db
from app.main import create_app
from app.services.prompt_service import (
    _PROMPTS_DIR,
    all_prompt_specs,
    clear_cache,
    load_prompt,
)

EXPECTED_VERSIONED = {
    "competitor_analyzer",
    "serp_synthesis",
    "evidence_research",
    "content_brief",
    "outline_generator",
    "outline_repair",
    "article_writer",
    "seo_reviewer",
    "fact_reviewer",
    "style_reviewer",
    "article_reviser",
    "image_planner",
}

RULEBOOKS = {"seo_article_guideline.md", "brand_visual_guideline.md"}


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


# ------------------------------------------------------------ inventory
def test_all_prompt_specs_lists_12_versioned_prompts():
    specs = all_prompt_specs()
    assert {s.name for s in specs} == EXPECTED_VERSIONED
    assert [s.name for s in specs] == sorted(EXPECTED_VERSIONED)
    for spec in specs:
        assert len(spec.prompt_hash) == 64
        int(spec.prompt_hash, 16)  # hex
        assert spec.version
    # The hash is the SHA256 of the FULL file (front matter included).
    writer = next(s for s in specs if s.name == "article_writer")
    assert writer.path.is_file()
    clear_cache()


def test_rulebooks_are_not_versioned():
    # The two rulebooks have no front matter → load_prompt rejects them.
    for name in RULEBOOKS:
        assert (_PROMPTS_DIR / name).is_file()
        with pytest.raises(ValueError):
            load_prompt(name[:-3])
    # ...but all_prompt_specs skips them rather than failing.
    specs = all_prompt_specs()
    assert not RULEBOOKS & {s.path.name for s in specs}


# ---------------------------------------------------------------- API
def test_api_prompts_inventory_shape(client: TestClient):
    resp = client.get("/api/prompts")
    assert resp.status_code == 200
    payload = resp.json()
    assert set(payload) == {"prompts", "stale_versions", "rulebooks"}
    by_name = {p["name"]: p for p in payload["prompts"]}
    assert set(by_name) == EXPECTED_VERSIONED
    for entry in payload["prompts"]:
        assert set(entry) == {
            "name", "version", "prompt_hash", "content_chars",
            "step", "usage", "jobs",
        }
        assert len(entry["prompt_hash"]) == 64
        assert entry["usage"] == 0 and entry["jobs"] == 0
        assert entry["content_chars"] > 0
    assert by_name["content_brief"]["step"] == 7
    assert by_name["image_planner"]["step"] == 14
    # Rulebooks: content hash only.
    assert {r["name"] for r in payload["rulebooks"]} == RULEBOOKS
    for r in payload["rulebooks"]:
        assert len(r["hash"]) == 64
    assert payload["stale_versions"] == []


def test_api_prompts_usage_and_stale(client: TestClient):
    """Seed provenance rows; verify aggregation and the drift case."""
    with client.db_session() as session:
        now = datetime.datetime.now(datetime.timezone.utc)
        j1 = GenerationJob(keyword="p9b2 kw one", status="ready")
        j2 = GenerationJob(keyword="p9b2 kw two", status="ready")
        session.add_all([j1, j2])
        session.flush()
        page = SourcePage(
            url="https://example.com/p9b2",
            normalized_url="https://example.com/p9b2",
            url_hash="c" * 64,
            domain="example.com",
            content_markdown="x",
            content_hash="d" * 64,
            extractor="fake",
            first_seen_at=now,
            last_fetched_at=now,
        )
        session.add(page)
        session.flush()

        comp = load_prompt("competitor_analyzer")
        writer = load_prompt("article_writer")
        sev = load_prompt("seo_reviewer")

        # Current competitor_analyzer hash on 2 jobs + a drifted (stale) row.
        session.add(CompetitorAnalysisRow(
            job_id=j1.id, source_page_id=page.id, analysis={}, model="m",
            prompt_version=comp.version, prompt_hash=comp.prompt_hash,
        ))
        session.add(CompetitorAnalysisRow(
            job_id=j2.id, source_page_id=page.id, analysis={}, model="m",
            prompt_version=comp.version, prompt_hash=comp.prompt_hash,
        ))
        session.add(CompetitorAnalysisRow(
            job_id=j1.id, source_page_id=page.id, analysis={}, model="m",
            prompt_version="0.9", prompt_hash="e" * 64,
        ))
        # Article version + review with matching hashes.
        av = ArticleVersionRow(
            job_id=j1.id, version=1, stage="writer", title="t",
            body_markdown="b", seo_title="s", meta_description="m",
            slug="p9b2", model="m", prompt_name="article_writer",
            prompt_version=writer.version, prompt_hash=writer.prompt_hash,
        )
        session.add(av)
        session.flush()
        session.add(ArticleReviewRow(
            job_id=j1.id, article_version_id=av.id, review_type="seo",
            review={}, model="m", prompt_version=sev.version,
            prompt_hash=sev.prompt_hash,
        ))
        # Image plan rows record planner provenance.
        ip = load_prompt("image_planner")
        session.add(ImageRow(
            job_id=j1.id, role="hero", sort_order=0, purpose="p",
            prompt="pr", filename="f", alt_text="a", aspect_ratio="16:9",
            provider="x", prompt_name=ip.name, prompt_version=ip.version,
            prompt_hash=ip.prompt_hash,
        ))
        session.commit()

    payload = client.get("/api/prompts").json()
    by_name = {p["name"]: p for p in payload["prompts"]}
    assert by_name["competitor_analyzer"]["usage"] == 2
    assert by_name["competitor_analyzer"]["jobs"] == 2
    assert by_name["article_writer"]["usage"] == 1
    assert by_name["article_writer"]["jobs"] == 1
    assert by_name["seo_reviewer"]["usage"] == 1
    assert by_name["image_planner"]["usage"] == 1
    assert by_name["fact_reviewer"]["usage"] == 0
    # The drifted row is surfaced, not mixed into the current version.
    stale = payload["stale_versions"]
    assert len(stale) == 1
    assert stale[0] == {
        "prompt_name": "competitor_analyzer",
        "prompt_version": "0.9",
        "prompt_hash": "e" * 64,
        "usage": 1,
        "jobs": 1,
    }


def test_api_prompts_ignores_incomplete_provenance(client: TestClient):
    """Legacy rows with NULL provenance must not crash the dashboard."""
    with client.db_session() as session:
        now = datetime.datetime.now(datetime.timezone.utc)
        job = GenerationJob(keyword="p9b2 incomplete provenance", status="ready")
        session.add(job)
        session.flush()
        page = SourcePage(
            url="https://example.com/p9b2-incomplete",
            normalized_url="https://example.com/p9b2-incomplete",
            url_hash="f" * 64,
            domain="example.com",
            content_markdown="x",
            content_hash="a" * 64,
            extractor="fake",
            first_seen_at=now,
            last_fetched_at=now,
        )
        session.add(page)
        session.flush()

        # Same name/version with and without a hash used to make sorted()
        # compare None to str. Image rows can also have a NULL prompt_name.
        session.add_all([
            CompetitorAnalysisRow(
                job_id=job.id, source_page_id=page.id, analysis={}, model="m",
                prompt_version="0.9", prompt_hash=None,
            ),
            CompetitorAnalysisRow(
                job_id=job.id, source_page_id=page.id, analysis={}, model="m",
                prompt_version="0.9", prompt_hash="b" * 64,
            ),
            ImageRow(
                job_id=job.id, role="hero", sort_order=0, purpose="p",
                prompt="pr", filename="f.png", alt_text="a",
                aspect_ratio="16:9", provider="x", prompt_name=None,
                prompt_version="0.9", prompt_hash="c" * 64,
            ),
        ])
        session.commit()

    api = client.get("/api/prompts")
    assert api.status_code == 200
    assert api.json()["stale_versions"] == [{
        "prompt_name": "competitor_analyzer",
        "prompt_version": "0.9",
        "prompt_hash": "b" * 64,
        "usage": 1,
        "jobs": 1,
    }]

    # The same payload feeds the HTML route; it must not fail in the
    # stale-hash display either.
    page_response = client.get("/prompts")
    assert page_response.status_code == 200


# -------------------------------------------------------------- web page
def test_prompts_page_renders(client: TestClient):
    resp = client.get("/prompts")
    assert resp.status_code == 200
    body = resp.text
    assert "Prompt versions" in body
    for name in EXPECTED_VERSIONED:
        assert name in body
    assert "Prompts" in body  # nav entry
