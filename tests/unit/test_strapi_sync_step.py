"""P7 unit: run_strapi_sync (spec sections 36-42, 64).

SQLite in-memory + a scriptable fake provider — no network. Verifies
the A-F order, idempotency (section 41), slug collision (section 42)
and the failure semantics (section 64: keep the documentId).
"""

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import JobStatus, StrapiSyncStatus
from app.core.exceptions import ErrorCode, PipelineError
from app.db.base import Base
from app.db.models import GenerationJob, StrapiSyncRow
from app.db.models.article import ArticleVersionRow
from app.db.models.images import ImageRow
from app.db.models.research import ContentBriefRow
from app.pipeline.steps.strapi_sync import run_strapi_sync
from app.schemas.strapi import MediaUploadResult, payload_hash

BASE = "http://strapi.test"

BODY = (
    "## What is anxious attachment\n\n"
    "First paragraph about the concept, long enough to anchor.\n\n"
    "## Why no contact feels impossible\n\n"
    "Second paragraph about the behavior, long enough to anchor.\n"
)

BRIEF = {
    "primary_keyword": "anxious attachment no contact",
    "secondary_keywords": ["anxious attachment"],
    "long_tail_keywords": ["why no contact is hard"],
    "search_intent": "informational",
    "article_strategy": "explainer",
}


class _Entry:
    def __init__(self, item_id, document_id, **attributes):
        self.id = item_id
        self.document_id = document_id
        self.slug = attributes.get("slug")
        self.title = attributes.get("title")
        self.status = attributes.get("status", "draft")
        self.body = attributes.get("body")
        self.meta_title = attributes.get("metaTitle")
        self.meta_description = attributes.get("metaDescription")
        self.seo_keywords = attributes.get("seoKeywords")
        self.author = attributes.get("author")
        self.category = attributes.get("category")
        self.main_image = attributes.get("mainImage")


class FakeStrapiProvider:
    """Records every call; the full A-F flow is scriptable per test."""

    def __init__(self, *, slug_entries=None, fail_inline: str | None = None):
        self.slug_entries = slug_entries or []
        self.fail_inline = fail_inline
        self.created: list[dict] = []
        self.updates: list[tuple[str, dict]] = []
        self.hero_uploads: list[dict] = []
        self.inline_uploads: list[dict] = []
        self.get_calls: list[str] = []
        self.slug_searches: list[str] = []

    async def find_blogs_by_slug(self, slug):
        self.slug_searches.append(slug)
        return list(self.slug_entries)

    async def create_draft_entry(self, payload):
        self.created.append(payload)
        data = payload["data"]
        return _Entry(
            42,
            "doc-42",
            slug=data.get("slug"),
            title=data.get("title"),
            status="draft",
            body=data.get("body"),
            metaTitle=data.get("metaTitle"),
            metaDescription=data.get("metaDescription"),
            seoKeywords=data.get("seoKeywords"),
            author=data.get("author"),
            category=data.get("category"),
            mainImage=None,
        )

    async def update_draft_entry(self, document_id, payload):
        self.updates.append((document_id, payload))
        data = payload.get("data") or {}
        return _Entry(
            42,
            document_id,
            slug="p7test-slug",
            title="T",
            status="draft",
            body=data.get("body", "previous"),
            metaTitle="mt",
            metaDescription="md",
            seoKeywords="kw",
            author="doc-author-1",
            category="doc-cat-1",
            mainImage=BASE + "/uploads/hero.webp",
        )

    async def get_draft(self, document_id):
        self.get_calls.append(document_id)
        last_body = self.updates[-1][1]["data"]["body"] if self.updates else BODY
        return _Entry(
            42,
            document_id,
            slug="p7test-slug",
            title="Anxious Attachment No Contact: P7 Test Guide",
            status="draft",
            body=last_body,
            metaTitle="mt",
            metaDescription="md",
            seoKeywords="anxious attachment no contact",
            author="doc-author-1",
            category="doc-cat-1",
            mainImage=BASE + "/uploads/hero.webp",
        )

    async def upload_hero(self, file_bytes, filename, *, blog_numeric_id,
                          alt_text="", caption=""):
        self.hero_uploads.append(
            {"bytes": file_bytes, "filename": filename, "blog_numeric_id": blog_numeric_id, "alt": alt_text}
        )
        return MediaUploadResult(
            media_id=101, url="/uploads/hero.webp",
            document_id="doc-media-101", alternative_text=alt_text or None,
        )

    async def upload_inline(self, file_bytes, filename, *, alt_text=""):
        self.inline_uploads.append(
            {"bytes": file_bytes, "filename": filename, "alt": alt_text}
        )
        if self.fail_inline and filename == self.fail_inline:
            raise PipelineError(
                ErrorCode.STRAPI_UPLOAD_FAILED, f"boom {filename}"
            )
        return MediaUploadResult(
            media_id=200 + len(self.inline_uploads),
            url=f"/uploads/{filename}",
            document_id=f"doc-media-{200 + len(self.inline_uploads)}",
            alternative_text=alt_text or None,
        )

    @property
    def call_count(self):
        return (
            len(self.slug_searches)
            + len(self.created)
            + len(self.updates)
            + len(self.hero_uploads)
            + len(self.inline_uploads)
        )


@pytest.fixture()
def db(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(GenerationJob(
            keyword="p7test anxious attachment no contact",
            status=JobStatus.READY.value,
            current_step="image_generating",
            target_function="coach",
            author_document_id=None,
            category_document_id=None,
        ))
        job = session.scalars(select(GenerationJob)).first()
        session.add(ContentBriefRow(job_id=job.id, brief=BRIEF))
        session.add(ArticleVersionRow(
            job_id=job.id,
            version=1,
            stage="revision",
            title="Anxious Attachment No Contact: P7 Test Guide",
            body_markdown=BODY,
            seo_title="Anxious Attachment and No Contact",
            meta_description="Why no contact feels so intense.",
            slug="p7test-slug",
        ))
        for name, (role, marker, heading, filename, alt) in {
            "hero": ("hero", None, None, "hero.webp", "Hero alt"),
            "i1": ("inline", "inline-1", "What is anxious attachment", "inline-1.webp", "Inline one alt"),
            "i2": ("inline", "inline-2", "Why no contact feels impossible", "inline-2.webp", "Inline two alt"),
        }.items():
            f = tmp_path / filename
            f.write_bytes(b"fake-image-bytes-" + name.encode())
            session.add(ImageRow(
                job_id=job.id,
                role=role,
                sort_order=0 if role == "hero" else (1 if marker == "inline-1" else 2),
                purpose="p",
                section_heading=heading,
                insertion_marker=marker,
                prompt="p",
                filename=filename,
                alt_text=alt,
                aspect_ratio="16:9" if role == "hero" else "4:3",
                local_path=str(f),
                mime_type="image/png",
                provider="fake",
                provider_request_id=f"fake-{name}",
            ))
        session.commit()
        yield session
    engine.dispose()


def _settings(tmp_path, **overrides) -> Settings:
    values = dict(
        llm_base_url="http://llm.test/v1",
        llm_api_key="test-key",
        llm_model="test-model",
        data_dir=str(tmp_path),
        strapi_base_url=BASE,
        strapi_api_token="unit-token",
        strapi_default_author_document_id="doc-author-1",
        strapi_default_category_document_id="doc-cat-1",
        _env_file=None,
    )
    values.update(overrides)
    return Settings(**values)


def _job(session):
    return session.scalars(select(GenerationJob)).first()


def _image_rows(session):
    return list(session.scalars(
        select(ImageRow).where(ImageRow.job_id == _job(session).id)
    ).all())


# ------------------------------------------------------------------- happy path
async def test_happy_path_creates_draft_and_verifies(db, tmp_path):
    provider = FakeStrapiProvider()
    settings = _settings(tmp_path)
    job = _job(db)

    row = await run_strapi_sync(db, job, provider, settings=settings)

    # job + sync row
    assert job.status == JobStatus.STRAPI_DRAFT_CREATED.value
    assert row.sync_status == StrapiSyncStatus.DRAFT_CREATED.value
    assert row.strapi_document_id == "doc-42"
    assert row.strapi_id == 42
    assert row.error_message is None
    assert row.last_payload_hash == payload_hash(row.last_payload)

    # STEP A: one create, relations + seoKeywords in the payload
    assert len(provider.created) == 1
    created = provider.created[0]["data"]
    assert created["author"] == "doc-author-1"
    assert created["category"] == "doc-cat-1"
    assert created["seoKeywords"] == (
        "anxious attachment no contact, anxious attachment, why no contact is hard"
    )
    assert created["slug"] == "p7test-slug"
    assert "postedAt" not in created  # default flag is false

    # STEP B: hero linked to the numeric id
    assert len(provider.hero_uploads) == 1
    assert provider.hero_uploads[0]["blog_numeric_id"] == 42

    # STEP C: both inline images
    assert len(provider.inline_uploads) == 2

    # STEP D/E: final body PUT — absolute URLs, no markers, no H1
    assert len(provider.updates) == 1
    doc_id, final = provider.updates[0]
    assert doc_id == "doc-42"
    assert set(final) == {"data"} and set(final["data"]) == {"body"}
    body = final["data"]["body"]
    assert BASE + "/uploads/inline-1.webp" in body
    assert BASE + "/uploads/inline-2.webp" in body
    assert "![Inline one alt](" in body
    assert "[[IMAGE:" not in body
    assert not any(line.startswith("# ") for line in body.splitlines())
    # default: hero NOT rendered into the body
    assert BASE + "/uploads/hero.webp" not in body

    # STEP F: GET verify
    assert provider.get_calls == ["doc-42"]

    # image rows carry the Strapi media columns
    rows = {r.filename: r for r in _image_rows(db)}
    assert rows["hero.webp"].strapi_media_id == 101
    assert rows["hero.webp"].strapi_url == BASE + "/uploads/hero.webp"
    assert rows["inline-1.webp"].strapi_media_id == 201
    assert rows["inline-1.webp"].strapi_url == BASE + "/uploads/inline-1.webp"
    assert rows["inline-2.webp"].strapi_media_document_id == "doc-media-202"


async def test_frontend_renders_main_image_false_prepends_hero(db, tmp_path):
    provider = FakeStrapiProvider()
    settings = _settings(tmp_path, strapi_frontend_renders_main_image=False)
    await run_strapi_sync(db, _job(db), provider, settings=settings)
    body = provider.updates[-1][1]["data"]["body"]
    assert body.startswith(f"![Hero alt]({BASE}/uploads/hero.webp)")


# --------------------------------------------------------------------- H07
# The final body PUT to Strapi must carry NO raw [[INTERNAL_LINK:*]] or
# [[IMAGE:*]] markers — the shared renderer (section 21 + 33) resolves both
# before the body leaves the pipeline.
async def test_h07_sync_body_resolves_internal_link_markers(db, tmp_path):
    from app.schemas.internal_link import InternalLinkRule as _Rule
    from app.services.internal_link_service import upsert_rule

    job = _job(db)
    upsert_rule(
        db,
        _Rule(
            marker="ATTACHMENT_TEST",
            anchor_text="attachment test",
            target_url="https://example.com/attachment-test",
        ),
    )
    version = db.scalars(select(ArticleVersionRow)).one()
    version.body_markdown = (
        "## What is anxious attachment\n\n"
        "First paragraph. Take the "
        "[[INTERNAL_LINK:ATTACHMENT_TEST]] "
        "before reaching out, long enough to anchor.\n\n"
        "## Why no contact feels impossible\n\n"
        "Second paragraph about the behavior, long enough to anchor.\n"
    )
    db.commit()

    provider = FakeStrapiProvider()
    await run_strapi_sync(db, job, provider, settings=_settings(tmp_path))

    # STEP E: the final body PUT carries the resolved link, no raw markers
    body = provider.updates[-1][1]["data"]["body"]
    assert "[attachment test](https://example.com/attachment-test)" in body
    assert "[[INTERNAL_LINK:" not in body
    assert "[[IMAGE:" not in body
    # image markers also resolved
    assert BASE + "/uploads/inline-1.webp" in body
    assert job.status == JobStatus.STRAPI_DRAFT_CREATED.value


async def test_h07_sync_unknown_internal_link_marker_fails(db, tmp_path):
    """H07: an unresolvable link marker blocks the sync — never PUT raw."""
    job = _job(db)
    version = db.scalars(select(ArticleVersionRow)).one()
    version.body_markdown = (
        "## What is anxious attachment\n\n"
        "First paragraph. "
        "[[INTERNAL_LINK:GHOST_MARKER]] "
        "that does not exist in the rules, long enough to anchor.\n\n"
        "## Why no contact feels impossible\n\n"
        "Second paragraph about the behavior, long enough to anchor.\n"
    )
    db.commit()

    provider = FakeStrapiProvider()
    with pytest.raises(PipelineError) as excinfo:
        await run_strapi_sync(db, job, provider, settings=_settings(tmp_path))

    assert excinfo.value.error_code == ErrorCode.ARTICLE_VALIDATION_FAILED
    # the final body PUT never happened (failure at STEP D)
    assert provider.updates == []
    # the draft + media were already checkpointed — documentId is kept
    assert job.status == JobStatus.STRAPI_SYNCING.value
    row = db.scalars(select(StrapiSyncRow)).first()
    assert row.sync_status == StrapiSyncStatus.FAILED.value
    assert row.strapi_document_id == "doc-42"


# ------------------------------------------------------------- slug collision
async def test_slug_conflict_other_entry_blocks_sync(db, tmp_path):
    other = _Entry(77, "doc-other", slug="p7test-slug", status="published")
    provider = FakeStrapiProvider(slug_entries=[other])
    settings = _settings(tmp_path)
    job = _job(db)

    with pytest.raises(PipelineError) as excinfo:
        await run_strapi_sync(db, job, provider, settings=settings)

    assert excinfo.value.error_code == ErrorCode.STRAPI_SLUG_CONFLICT
    # no draft was created, no uploads
    assert provider.created == []
    assert provider.hero_uploads == []
    # failure persisted with the job's error fields
    assert job.error_code == "STRAPI_SLUG_CONFLICT"
    assert "doc-other" in job.error_message
    assert db.scalars(select(StrapiSyncRow)).first() is None


# ----------------------------------------------------------- mid-sync failure
async def test_mid_upload_failure_keeps_document_id(db, tmp_path):
    provider = FakeStrapiProvider(fail_inline="inline-2.webp")
    settings = _settings(tmp_path)
    job = _job(db)

    with pytest.raises(PipelineError) as excinfo:
        await run_strapi_sync(db, job, provider, settings=settings)

    assert excinfo.value.error_code == ErrorCode.STRAPI_UPLOAD_FAILED
    assert job.status == JobStatus.STRAPI_SYNCING.value  # not created
    assert job.error_code == "STRAPI_UPLOAD_FAILED"
    row = db.scalars(select(StrapiSyncRow)).first()
    assert row.sync_status == StrapiSyncStatus.FAILED.value
    # section 64: documentId MUST survive for the retry
    assert row.strapi_document_id == "doc-42"
    assert row.strapi_id == 42
    # hero (before the failure) was recorded; the failing inline was not
    rows = {r.filename: r for r in _image_rows(db)}
    assert rows["hero.webp"].strapi_media_id == 101
    assert rows["inline-1.webp"].strapi_media_id == 201
    assert rows["inline-2.webp"].strapi_media_id is None


# ------------------------------------------------------------------- idempotency
async def test_retry_updates_existing_draft_without_new_create(db, tmp_path):
    job = _job(db)
    # previous attempt failed mid-sync but kept the documentId (section 64)
    rows = _image_rows(db)
    for r in rows:
        r.strapi_media_id = 999
        r.strapi_media_document_id = "doc-media-999"
        r.strapi_url = BASE + f"/uploads/{r.filename}"
    db.add(StrapiSyncRow(
        job_id=job.id,
        strapi_id=42,
        strapi_document_id="doc-42",
        sync_status=StrapiSyncStatus.FAILED.value,
        error_message="previous upload failure",
    ))
    db.commit()

    own_entry = _Entry(42, "doc-42", slug="p7test-slug", status="draft")
    provider = FakeStrapiProvider(slug_entries=[own_entry])
    settings = _settings(tmp_path)

    row = await run_strapi_sync(db, job, provider, settings=settings)

    assert provider.created == []  # NO second draft (section 41)
    assert row.strapi_document_id == "doc-42"
    assert row.sync_status == StrapiSyncStatus.DRAFT_CREATED.value
    assert job.status == JobStatus.STRAPI_DRAFT_CREATED.value
    # media already uploaded — no re-upload
    assert provider.hero_uploads == []
    assert provider.inline_uploads == []
    # but the final body PUT + GET verify still ran
    # (STEP A updates the existing draft, STEP E writes the final body)
    assert len(provider.updates) == 2
    assert provider.updates[0][0] == "doc-42"
    assert set(provider.updates[1][1]["data"]) == {"body"}
    assert provider.get_calls == ["doc-42"]


async def test_retry_with_foreign_slug_still_conflicts(db, tmp_path):
    job = _job(db)
    db.add(StrapiSyncRow(
        job_id=job.id,
        strapi_id=42,
        strapi_document_id="doc-42",
        sync_status=StrapiSyncStatus.FAILED.value,
    ))
    db.commit()
    # slug now owned by ANOTHER entry (our draft was deleted externally)
    other = _Entry(77, "doc-other", slug="p7test-slug", status="draft")
    provider = FakeStrapiProvider(slug_entries=[other])
    with pytest.raises(PipelineError) as excinfo:
        await run_strapi_sync(db, _job(db), provider, settings=_settings(tmp_path))
    assert excinfo.value.error_code == ErrorCode.STRAPI_SLUG_CONFLICT
    assert provider.created == []


# ------------------------------------------------------------------- pre-checks
async def test_missing_author_fails_before_any_http(db, tmp_path):
    job = _job(db)
    job.author_document_id = None
    provider = FakeStrapiProvider()
    settings = _settings(tmp_path, strapi_default_author_document_id="")

    with pytest.raises(PipelineError) as excinfo:
        await run_strapi_sync(db, job, provider, settings=settings)

    assert excinfo.value.error_code == ErrorCode.STRAPI_SCHEMA_MISMATCH
    assert "author" in excinfo.value.message.lower()
    assert provider.call_count == 0  # ZERO Strapi HTTP calls
    assert job.error_code == "STRAPI_SCHEMA_MISMATCH"
    assert job.status == JobStatus.STRAPI_SYNCING.value  # not created


async def test_job_override_beats_default(db, tmp_path):
    job = _job(db)
    job.author_document_id = "doc-job-author"
    provider = FakeStrapiProvider()
    await run_strapi_sync(db, job, provider, settings=_settings(tmp_path))
    assert provider.created[0]["data"]["author"] == "doc-job-author"
    assert provider.created[0]["data"]["category"] == "doc-cat-1"
