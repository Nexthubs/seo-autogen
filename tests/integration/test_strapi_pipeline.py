"""P7 integration: strapi_syncs table + run_strapi_sync on PostgreSQL
(spec sections 41, 46.16, 59.3, 64).

Part 1 (always, when PostgreSQL is reachable): the full A-F sync flow
against the real ``strapi_syncs`` table with a fake provider — proves
the table shape (46.16), the unique job_id constraint, JSONB payload
storage, and the failure/idempotency semantics (section 64).

Part 2 (only with RUN_EXTERNAL_INTEGRATION_TESTS=true and a real
Strapi configured): the section-59.3 flow against live Strapi using
the dedicated test slug ``seo-auto-integration-test-{uuid}``:
create draft -> upload image -> attach mainImage -> update body ->
GET verify -> delete test draft. **Never publishes.**

Shared integration DB discipline: every test only creates rows with
``p7test`` markers and deletes exactly those rows in teardown.
"""

import json
import os
import uuid

import httpx
import pytest
from sqlalchemy import delete as sa_delete, inspect, select

from app.core.config import Settings
from app.core.enums import JobStatus, StrapiSyncStatus
from app.core.exceptions import PipelineError
from app.db.models import GenerationJob, StrapiSyncRow
from app.db.models.article import ArticleVersionRow
from app.db.models.images import ImageRow
from app.db.models.research import ContentBriefRow
from app.db.session import SessionLocal, check_database, engine
from app.pipeline.steps.strapi_sync import run_strapi_sync
from app.schemas.strapi import payload_hash

from tests.unit.test_strapi_sync_step import (
    BODY,
    BRIEF,
    FakeStrapiProvider,
)

pytestmark = pytest.mark.skipif(
    not check_database(), reason="PostgreSQL not reachable"
)

KEYWORD = "p7test strapi sync integration"
SLUG = "p7test-strapi-sync"


def _settings(data_dir: str) -> Settings:
    return Settings(
        llm_base_url="http://llm.test/v1",
        llm_api_key="test-key",
        llm_model="test-model",
        data_dir=data_dir,
        strapi_base_url="http://strapi.fake",
        strapi_api_token="integration-token",
        strapi_default_author_document_id="doc-author-1",
        strapi_default_category_document_id="doc-cat-1",
        _env_file=None,
    )


def _clean_p7test() -> None:
    """Delete only the p7test rows created by this test file."""
    with SessionLocal() as session:
        job_ids = [
            j.id
            for j in session.scalars(
                select(GenerationJob).where(
                    GenerationJob.keyword.startswith("p7test")
                )
            ).all()
        ]
        if not job_ids:
            return
        session.execute(
            sa_delete(StrapiSyncRow).where(
                StrapiSyncRow.job_id.in_(job_ids)
            )
        )
        session.execute(
            sa_delete(ImageRow).where(ImageRow.job_id.in_(job_ids))
        )
        session.execute(
            sa_delete(ArticleVersionRow).where(
                ArticleVersionRow.job_id.in_(job_ids)
            )
        )
        session.execute(
            sa_delete(ContentBriefRow).where(
                ContentBriefRow.job_id.in_(job_ids)
            )
        )
        session.execute(
            sa_delete(GenerationJob).where(
                GenerationJob.keyword.startswith("p7test")
            )
        )
        session.commit()


@pytest.fixture()
def db(tmp_path):
    _clean_p7test()
    with SessionLocal() as session:
        yield session
    _clean_p7test()


def _seed(db, tmp_path) -> GenerationJob:
    job = GenerationJob(
        keyword=KEYWORD,
        status=JobStatus.READY.value,
        current_step="image_generating",
        target_function="coach",
    )
    db.add(job)
    db.flush()
    db.add(ContentBriefRow(job_id=job.id, brief=BRIEF))
    db.add(
        ArticleVersionRow(
            job_id=job.id,
            version=1,
            stage="revision",
            title="P7 Integration Sync Guide",
            body_markdown=BODY,
            seo_title="P7 Integration Sync Guide",
            meta_description="Why no contact feels so intense.",
            slug=SLUG,
        )
    )
    for marker, filename, heading in (
        (None, "hero.webp", None),
        ("inline-1", "inline-1.webp", "What is anxious attachment"),
        ("inline-2", "inline-2.webp", "Why no contact feels impossible"),
    ):
        f = tmp_path / filename
        f.write_bytes(b"integration-image-bytes")
        db.add(
            ImageRow(
                job_id=job.id,
                role="hero" if marker is None else "inline",
                sort_order=0 if marker is None else (1 if marker == "inline-1" else 2),
                purpose="p",
                section_heading=heading,
                insertion_marker=marker,
                prompt="p",
                filename=filename,
                alt_text=f"alt {filename}",
                aspect_ratio="16:9" if marker is None else "4:3",
                local_path=str(f),
                mime_type="image/png",
                provider="fake",
                provider_request_id=f"fake-{filename}",
            )
        )
    db.commit()
    return job


# ------------------------------------------------- strapi_syncs table (46.16)
def test_strapi_syncs_table_shape(db):
    columns = {c["name"] for c in inspect(engine).get_columns("strapi_syncs")}
    assert {
        "id",
        "job_id",
        "strapi_id",
        "strapi_document_id",
        "sync_status",
        "last_payload",
        "last_payload_hash",
        "error_message",
        "created_at",
        "updated_at",
    } <= columns
    # job_id unique (one sync row per job, section 41)
    db.add(
        GenerationJob(
            keyword=KEYWORD + "-dupcheck",
            status=JobStatus.QUEUED.value,
        )
    )
    db.flush()
    job = db.scalars(select(GenerationJob).where(
        GenerationJob.keyword == KEYWORD + "-dupcheck"
    )).first()
    db.add(StrapiSyncRow(job_id=job.id, sync_status="pending"))
    db.commit()
    with pytest.raises(Exception):
        db.add(StrapiSyncRow(job_id=job.id, sync_status="pending"))
        db.commit()
    db.rollback()


# ------------------------------------------------- full A-F flow on PostgreSQL
async def test_happy_path_on_postgres(db, tmp_path):
    job = _seed(db, tmp_path)
    provider = FakeStrapiProvider()
    row = await run_strapi_sync(
        db, job, provider, settings=_settings(str(tmp_path))
    )

    assert row.sync_status == StrapiSyncStatus.DRAFT_CREATED.value
    assert job.status == JobStatus.STRAPI_DRAFT_CREATED.value
    # JSONB round-trip: the stored payload is a real dict
    assert isinstance(row.last_payload, dict)
    assert row.last_payload_hash == payload_hash(row.last_payload)
    images = {
        r.filename: r
        for r in db.scalars(
            select(ImageRow).where(ImageRow.job_id == job.id)
        ).all()
    }
    assert images["hero.webp"].strapi_media_id == 101
    assert images["hero.webp"].strapi_url == "http://strapi.fake/uploads/hero.webp"


async def test_failure_keeps_document_id_on_postgres(db, tmp_path):
    job = _seed(db, tmp_path)
    provider = FakeStrapiProvider(fail_inline="inline-2.webp")
    with pytest.raises(PipelineError):
        await run_strapi_sync(
            db, job, provider, settings=_settings(str(tmp_path))
        )
    row = db.scalars(
        select(StrapiSyncRow).where(StrapiSyncRow.job_id == job.id)
    ).first()
    assert row.sync_status == StrapiSyncStatus.FAILED.value
    assert row.strapi_document_id == "doc-42"  # section 64: retry-safe
    db.refresh(job)
    assert job.status == JobStatus.STRAPI_SYNCING.value
    assert job.error_code == "STRAPI_UPLOAD_FAILED"


# ============================================== section 59.3 against live Strapi
SKIP_EXTERNAL = pytest.mark.skipif(
    os.environ.get("RUN_EXTERNAL_INTEGRATION_TESTS") != "true",
    reason="set RUN_EXTERNAL_INTEGRATION_TESTS=true to run against a real Strapi",
)


def _external_settings() -> Settings:
    required = {
        "STRAPI_BASE_URL": os.environ.get("STRAPI_BASE_URL"),
        "STRAPI_API_TOKEN": os.environ.get("STRAPI_API_TOKEN"),
        "STRAPI_DEFAULT_AUTHOR_DOCUMENT_ID": os.environ.get(
            "STRAPI_DEFAULT_AUTHOR_DOCUMENT_ID"
        ),
        "STRAPI_DEFAULT_CATEGORY_DOCUMENT_ID": os.environ.get(
            "STRAPI_DEFAULT_CATEGORY_DOCUMENT_ID"
        ),
    }
    if any(not v for v in required.values()):
        pytest.skip("live Strapi configuration is incomplete")
    return Settings(
        llm_base_url="http://llm.test/v1",
        llm_api_key="test-key",
        llm_model="test-model",
        data_dir="/tmp/p7-external",
        strapi_base_url=required["STRAPI_BASE_URL"],
        strapi_api_token=required["STRAPI_API_TOKEN"],
        strapi_default_author_document_id=required[
            "STRAPI_DEFAULT_AUTHOR_DOCUMENT_ID"
        ],
        strapi_default_category_document_id=required[
            "STRAPI_DEFAULT_CATEGORY_DOCUMENT_ID"
        ],
        _env_file=None,
    )


@SKIP_EXTERNAL
async def test_external_strapi_59_3_flow():
    """Section 59.3: create -> upload -> attach mainImage -> update body
    -> GET verify -> delete test draft. The test slug is
    ``seo-auto-integration-test-{uuid}``; publish is never exercised.
    """
    settings = _external_settings()
    slug = f"seo-auto-integration-test-{uuid.uuid4()}"
    headers = {"Authorization": f"Bearer {settings.strapi_api_token}"}
    png = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0dIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\x0dIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
        b"\x05\xfe\xdc\xcc\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    async with httpx.AsyncClient(
        base_url=settings.strapi_base_url, timeout=30
    ) as client:
        # 1. Create draft
        resp = await client.post(
            "/api/blogs",
            params={"status": "draft"},
            headers=headers,
            json={
                "data": {
                    "title": "SEO Auto Integration Test",
                    "slug": slug,
                    "body": "## Intro\n\nTemporary body.",
                    "metaTitle": "SEO Auto Integration Test",
                    "metaDescription": "Created by the P7 integration test.",
                    "seoKeywords": "p7 integration test",
                    "author": settings.strapi_default_author_document_id,
                    "category": settings.strapi_default_category_document_id,
                }
            },
        )
        assert resp.status_code in (200, 201), resp.text[:300]
        data = resp.json()["data"]
        document_id = data["documentId"]
        numeric_id = data["id"]

        try:
            # 2. Upload a small test image (hero, bound to the draft)
            resp = await client.post(
                "/api/upload",
                headers=headers,
                files={
                    "files": (
                        "test-image.png", png, "image/png"
                    )
                },
                data={
                    "ref": settings.strapi_blog_uid,
                    "refId": str(numeric_id),
                    "field": "mainImage",
                    "fileInfo": json.dumps(
                        {
                            "name": "test-image",
                            "alternativeText": "P7 integration test image",
                            "caption": None,
                        }
                    ),
                },
            )
            assert resp.status_code in (200, 201), resp.text[:300]
            media = resp.json()["data"]["data"]
            main_image = media["url"]

            # 3+4. Attach mainImage and update the body
            resp = await client.put(
                f"/api/blogs/{document_id}",
                params={"status": "draft"},
                headers=headers,
                json={
                    "data": {
                        "body": "## Intro\n\nFinal body.",
                        "mainImage": main_image,
                    }
                },
            )
            assert resp.status_code == 200, resp.text[:300]

            # 5. GET draft
            resp = await client.get(
                f"/api/blogs/{document_id}",
                params={"status": "draft"},
                headers=headers,
            )
            assert resp.status_code == 200, resp.text[:300]
            draft = resp.json()["data"]["attributes"]
            # 6. Verify status + fields
            assert draft["status"] == "draft"
            assert draft["slug"] == slug
            assert draft["body"] == "## Intro\n\nFinal body."
            assert draft["mainImage"] == main_image
        finally:
            # 7. Delete the test draft (section 59.3 cleanup only —
            # the provider itself never deletes; the test token owns it)
            await client.delete(
                f"/api/blogs/{document_id}", headers=headers
            )
