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
    """Records every call; the full A-F flow is scriptable per test.

    M02: the fake keeps the draft's DOCUMENT STATE — create/updates
    merge their ``data`` fields into it, and ``get_draft`` ECHOES that
    state (plus the uploaded hero mainImage). The STEP F verification
    therefore passes only when the step pushes values that actually
    survive on the "server"; ``get_override`` can corrupt any field to
    prove a mismatch is rejected.
    """

    def __init__(
        self,
        *,
        slug_entries=None,
        fail_inline: str | None = None,
        main_image: str | None = None,
        get_override: dict | None = None,
    ):
        self.slug_entries = slug_entries or []
        self.fail_inline = fail_inline
        self.main_image = main_image  # set after the hero upload
        self.get_override = get_override or {}
        self.created: list[dict] = []
        self.updates: list[tuple[str, dict]] = []
        self.hero_uploads: list[dict] = []
        self.inline_uploads: list[dict] = []
        self.get_calls: list[str] = []
        self.slug_searches: list[str] = []
        self._doc_state: dict = {}

    def _doc_view(self) -> dict:
        view = {
            "slug": self._doc_state.get("slug"),
            "title": self._doc_state.get("title"),
            "status": "draft",
            "body": self._doc_state.get("body"),
            "metaTitle": self._doc_state.get("metaTitle"),
            "metaDescription": self._doc_state.get("metaDescription"),
            "seoKeywords": self._doc_state.get("seoKeywords"),
            "author": self._doc_state.get("author"),
            "category": self._doc_state.get("category"),
            "mainImage": self.main_image,
        }
        view.update(self.get_override)
        return view

    async def find_blogs_by_slug(self, slug):
        self.slug_searches.append(slug)
        return list(self.slug_entries)

    async def create_draft_entry(self, payload):
        self.created.append(payload)
        self._doc_state = dict(payload["data"])
        return _Entry(42, "doc-42", **self._doc_view())

    async def update_draft_entry(self, document_id, payload):
        self.updates.append((document_id, payload))
        self._doc_state.update(payload.get("data") or {})
        return _Entry(42, document_id, **self._doc_view())

    async def get_draft(self, document_id):
        self.get_calls.append(document_id)
        return _Entry(42, document_id, **self._doc_view())

    async def upload_hero(self, file_bytes, filename, *, blog_numeric_id,
                          alt_text="", caption=""):
        self.hero_uploads.append(
            {"bytes": file_bytes, "filename": filename, "blog_numeric_id": blog_numeric_id, "alt": alt_text}
        )
        self.main_image = BASE + "/uploads/hero.webp"
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
    # M01: the draft + media were already checkpointed — the documentId
    # is kept and the job lands in the UNIFIED strapi_sync_failed state
    assert job.status == JobStatus.STRAPI_SYNC_FAILED.value
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
    # M01: the pre-HTTP failure is persisted in the UNIFIED state — a
    # sync row with sync_status=failed + the job in strapi_sync_failed,
    # so the UI shows it and the sync retry endpoint can pick it up.
    assert job.status == JobStatus.STRAPI_SYNC_FAILED.value
    assert job.error_code == "STRAPI_SLUG_CONFLICT"
    assert "doc-other" in job.error_message
    row = db.scalars(select(StrapiSyncRow)).first()
    assert row is not None
    assert row.sync_status == StrapiSyncStatus.FAILED.value
    assert row.strapi_document_id is None  # nothing was created yet


# ----------------------------------------------------------- mid-sync failure
async def test_mid_upload_failure_keeps_document_id(db, tmp_path):
    provider = FakeStrapiProvider(fail_inline="inline-2.webp")
    settings = _settings(tmp_path)
    job = _job(db)

    with pytest.raises(PipelineError) as excinfo:
        await run_strapi_sync(db, job, provider, settings=settings)

    assert excinfo.value.error_code == ErrorCode.STRAPI_UPLOAD_FAILED
    # M01: unified persisted state — the job is strapi_sync_failed
    assert job.status == JobStatus.STRAPI_SYNC_FAILED.value
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
    # the hero was already uploaded on the previous attempt — the
    # "server" already has it as the entry's mainImage
    provider = FakeStrapiProvider(
        slug_entries=[own_entry], main_image=BASE + "/uploads/hero.webp"
    )
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
    # M01: the first-precheck failure is persisted in the UNIFIED state
    assert job.status == JobStatus.STRAPI_SYNC_FAILED.value
    row = db.scalars(select(StrapiSyncRow)).first()
    assert row is not None
    assert row.sync_status == StrapiSyncStatus.FAILED.value
    assert row.strapi_document_id is None


async def test_job_override_beats_default(db, tmp_path):
    job = _job(db)
    job.author_document_id = "doc-job-author"
    provider = FakeStrapiProvider()
    await run_strapi_sync(db, job, provider, settings=_settings(tmp_path))
    assert provider.created[0]["data"]["author"] == "doc-job-author"
    assert provider.created[0]["data"]["category"] == "doc-cat-1"


# ------------------------------------------------------------- M01: unified failed state
def test_sync_failed_state_is_not_terminal():
    """Section 64: strapi_sync_failed is PERSISTED but not pipeline-terminal
    — a full/step retry must stay 409; only the sync retry path applies."""
    from app.core.enums import JobStatus as JS

    assert JS.STRAPI_SYNC_FAILED is not None
    assert not JS.STRAPI_SYNC_FAILED.is_terminal
    # while ready/failed/cancelled are terminal
    assert JS.READY.is_terminal
    assert JS.FAILED.is_terminal
    assert JS.CANCELLED.is_terminal


async def test_missing_local_file_fails_with_upload_error(db, tmp_path):
    """M01: a local image path that no longer resolves (file vanished) is a
    STRAPI_UPLOAD_FAILED — not an unhandled OSError — and lands in the
    unified strapi_sync_failed state, keeping any documentId."""
    job = _job(db)
    # Point the hero at a path that does not exist on disk.
    rows = _image_rows(db)
    hero = next(r for r in rows if r.role == "hero")
    hero.local_path = str(tmp_path / "does-not-exist-hero.webp")
    db.commit()

    provider = FakeStrapiProvider()
    with pytest.raises(PipelineError) as excinfo:
        await run_strapi_sync(db, job, provider, settings=_settings(tmp_path))

    assert excinfo.value.error_code == ErrorCode.STRAPI_UPLOAD_FAILED
    assert job.status == JobStatus.STRAPI_SYNC_FAILED.value
    assert job.error_code == "STRAPI_UPLOAD_FAILED"
    row = db.scalars(select(StrapiSyncRow)).first()
    assert row is not None
    assert row.sync_status == StrapiSyncStatus.FAILED.value


async def test_mid_failure_then_retry_updates_same_draft(db, tmp_path):
    """M01: a mid-sync failure persists strapi_sync_failed + documentId;
    the subsequent sync retry UPDATES the same draft (no second create)."""
    job = _job(db)
    # First attempt: fail on inline-2 upload.
    provider1 = FakeStrapiProvider(fail_inline="inline-2.webp")
    with pytest.raises(PipelineError):
        await run_strapi_sync(db, job, provider1, settings=_settings(tmp_path))
    assert job.status == JobStatus.STRAPI_SYNC_FAILED.value

    # Second attempt (retry): everything succeeds. It must NOT create a
    # second draft, and must reuse the stored documentId. The hero was
    # uploaded on attempt 1, so the "server" already has its mainImage.
    own_entry = _Entry(42, "doc-42", slug="p7test-slug", status="draft")
    provider2 = FakeStrapiProvider(
        slug_entries=[own_entry], main_image=BASE + "/uploads/hero.webp"
    )
    row = await run_strapi_sync(db, job, provider2, settings=_settings(tmp_path))

    assert provider2.created == []  # NO second draft
    assert row.strapi_document_id == "doc-42"
    assert row.sync_status == StrapiSyncStatus.DRAFT_CREATED.value
    assert job.status == JobStatus.STRAPI_DRAFT_CREATED.value


# ------------------------------------------------------------- M02: normalized verify
async def test_verify_rejects_wrong_title(db, tmp_path):
    """M02: a GET verify that reads back a DIFFERENT title is a schema
    mismatch (WRONG TITLE must be rejected), keeping the documentId."""
    job = _job(db)
    provider = FakeStrapiProvider(get_override={"title": "WRONG TITLE"})
    with pytest.raises(PipelineError) as excinfo:
        await run_strapi_sync(db, job, provider, settings=_settings(tmp_path))
    assert excinfo.value.error_code == ErrorCode.STRAPI_SCHEMA_MISMATCH
    assert "title mismatch" in excinfo.value.message
    row = db.scalars(select(StrapiSyncRow)).first()
    assert row.sync_status == StrapiSyncStatus.FAILED.value
    assert row.strapi_document_id == "doc-42"
    assert job.status == JobStatus.STRAPI_SYNC_FAILED.value


async def test_verify_rejects_published_status(db, tmp_path):
    """M02: a GET verify that reads back status='published' is rejected —
    this tool never publishes, so a published read-back is a mismatch."""
    job = _job(db)
    provider = FakeStrapiProvider(get_override={"status": "published"})
    with pytest.raises(PipelineError) as excinfo:
        await run_strapi_sync(db, job, provider, settings=_settings(tmp_path))
    assert excinfo.value.error_code == ErrorCode.STRAPI_SCHEMA_MISMATCH
    assert "status" in excinfo.value.message
    assert job.status == JobStatus.STRAPI_SYNC_FAILED.value


async def test_verify_accepts_populated_relation_objects(db, tmp_path):
    """M02/H03: relations come back as POPULATED objects (author/category)
    and the media as an object — the normalized comparison still passes
    when the documentId matches the expected relation."""
    job = _job(db)
    provider = FakeStrapiProvider(
        get_override={
            "author": {"id": 5, "documentId": "doc-author-1", "name": "Alice"},
            "category": {"id": 9, "documentId": "doc-cat-1", "name": "Health"},
            "mainImage": {
                "id": 101,
                "documentId": "doc-media-101",
                "url": "/uploads/hero.webp",
            },
        }
    )
    row = await run_strapi_sync(db, job, provider, settings=_settings(tmp_path))
    assert row.sync_status == StrapiSyncStatus.DRAFT_CREATED.value
    assert job.status == JobStatus.STRAPI_DRAFT_CREATED.value


async def test_verify_rejects_wrong_relation_document_id(db, tmp_path):
    """M02: a GET verify that resolves author to a DIFFERENT documentId is a
    mismatch (relation values must match what we pushed)."""
    job = _job(db)
    provider = FakeStrapiProvider(
        get_override={"author": "doc-someone-else"}
    )
    with pytest.raises(PipelineError) as excinfo:
        await run_strapi_sync(db, job, provider, settings=_settings(tmp_path))
    assert excinfo.value.error_code == ErrorCode.STRAPI_SCHEMA_MISMATCH
    assert "author mismatch" in excinfo.value.message
    assert job.status == JobStatus.STRAPI_SYNC_FAILED.value


async def test_verify_rejects_wrong_media(db, tmp_path):
    """M02: a GET verify that reads back a DIFFERENT mainImage is a mismatch
    (media id/url must match the uploaded hero)."""
    job = _job(db)
    provider = FakeStrapiProvider(
        get_override={"mainImage": BASE + "/uploads/other-hero.webp"}
    )
    with pytest.raises(PipelineError) as excinfo:
        await run_strapi_sync(db, job, provider, settings=_settings(tmp_path))
    assert excinfo.value.error_code == ErrorCode.STRAPI_SCHEMA_MISMATCH
    assert "mainImage mismatch" in excinfo.value.message
    assert job.status == JobStatus.STRAPI_SYNC_FAILED.value


async def test_verify_accepts_absolute_url_main_image(db, tmp_path):
    """M02/H03: mainImage comparison is PATH-based — a fully-absolute URL
    with the same path still matches the stored relative upload path."""
    job = _job(db)
    provider = FakeStrapiProvider(
        get_override={"mainImage": "https://cms.test/uploads/hero.webp"}
    )
    row = await run_strapi_sync(db, job, provider, settings=_settings(tmp_path))
    assert row.sync_status == StrapiSyncStatus.DRAFT_CREATED.value
    assert job.status == JobStatus.STRAPI_DRAFT_CREATED.value


# ============================================================ R-H02: any exception
class _CrashingProvider(FakeStrapiProvider):
    """Raises a NON-PipelineError from the hero upload (e.g. an unexpected
    provider payload shape)."""

    async def upload_hero(self, *args, **kwargs):
        raise RuntimeError("unexpected payload shape")


async def test_r_h02_unexpected_error_lands_in_failed_state(db, tmp_path):
    """R-H02: a non-PipelineError must still persist the unified failure state
    (job strapi_sync_failed + sync row FAILED + documentId kept), not leave the
    job stranded in ``strapi_syncing`` with the worker merely logging a crash."""
    job = _job(db)
    provider = _CrashingProvider()

    with pytest.raises(RuntimeError):
        await run_strapi_sync(db, job, provider, settings=_settings(tmp_path))

    assert job.status == JobStatus.STRAPI_SYNC_FAILED.value
    assert job.error_code == "UNEXPECTED"
    assert job.error_raw  # full traceback retained (redacted)
    assert "RuntimeError" in job.error_message
    row = db.scalars(select(StrapiSyncRow)).first()
    assert row is not None
    assert row.sync_status == StrapiSyncStatus.FAILED.value
    # the draft was created before the crash: the retry anchor is preserved
    assert row.strapi_document_id == "doc-42"


# ============================================================ R-H03: failed row retry
async def test_r_h03_precheck_failure_then_retry_reuses_row(db, tmp_path):
    """R-H03 (audit reproduction): first attempt fails the author pre-check and
    persists a FAILED row with ``documentId=None``. After fixing the config, the
    retry creates the draft and writes the id back onto the SAME row — no
    UNIQUE(job_id) violation, no second local row."""
    job = _job(db)
    job.author_document_id = None
    failing = FakeStrapiProvider()
    settings = _settings(tmp_path, strapi_default_author_document_id="")

    with pytest.raises(PipelineError):
        await run_strapi_sync(db, job, failing, settings=settings)
    first = db.scalars(select(StrapiSyncRow)).all()
    assert len(first) == 1
    assert first[0].strapi_document_id is None
    assert first[0].sync_status == StrapiSyncStatus.FAILED.value

    # Fix the config and retry with a fresh provider.
    job.author_document_id = "doc-author-1"
    db.commit()
    good = FakeStrapiProvider()
    row = await run_strapi_sync(db, job, good, settings=_settings(tmp_path))

    rows = db.scalars(select(StrapiSyncRow)).all()
    assert len(rows) == 1, "retry must not create a duplicate strapi_syncs row"
    assert rows[0].id == first[0].id
    assert row.strapi_document_id == "doc-42"
    assert len(good.created) == 1
    assert job.status == JobStatus.STRAPI_DRAFT_CREATED.value


async def test_r_h03_create_http_failure_then_retry_succeeds(db, tmp_path):
    """R-H03: an HTTP create failure (no documentId stored) followed by a
    successful retry reuses the same local row."""

    class _CreateFailing(FakeStrapiProvider):
        async def create_draft_entry(self, payload):
            self.created.append(payload)
            raise PipelineError(
                ErrorCode.STRAPI_DRAFT_CREATE_FAILED, "create HTTP 500"
            )

    job = _job(db)
    with pytest.raises(PipelineError):
        await run_strapi_sync(
            db, job, _CreateFailing(), settings=_settings(tmp_path)
        )
    rows = db.scalars(select(StrapiSyncRow)).all()
    assert len(rows) == 1
    assert rows[0].strapi_document_id is None
    assert rows[0].sync_status == StrapiSyncStatus.FAILED.value

    provider = FakeStrapiProvider()
    row = await run_strapi_sync(db, job, provider, settings=_settings(tmp_path))
    assert len(db.scalars(select(StrapiSyncRow)).all()) == 1
    assert row.strapi_document_id == "doc-42"
    assert job.status == JobStatus.STRAPI_DRAFT_CREATED.value


async def test_r_h02_full_sync_with_real_provider_standard_201_array(db, tmp_path):
    """R-H02: end-to-end A–F sync through the REAL ``StrapiCMSProvider`` whose
    MockTransport answers with STANDARD Strapi 5 shapes — including the
    top-level upload ARRAY with HTTP 201 that used to crash the parser."""
    import httpx

    from app.providers.cms.strapi_cms import StrapiCMSProvider

    state: dict = {"seq": 0, "doc": None, "main_image": None}

    def _flat() -> dict:
        doc = state["doc"] or {}
        return {
            "id": 42,
            "documentId": "doc-42",
            "status": "draft",
            "slug": doc.get("slug"),
            "title": doc.get("title"),
            "body": doc.get("body"),
            "metaTitle": doc.get("metaTitle"),
            "metaDescription": doc.get("metaDescription"),
            "seoKeywords": doc.get("seoKeywords"),
            "author": {"id": 1, "documentId": doc.get("author")},
            "category": {"id": 2, "documentId": doc.get("category")},
            "mainImage": state["main_image"],
        }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if method == "GET" and path == "/api/blogs":
            return httpx.Response(200, json={"data": [], "meta": {}})
        if method == "POST" and path == "/api/blogs":
            state["doc"] = dict(json.loads(request.content)["data"])
            return httpx.Response(201, json={"data": _flat()})
        if method == "POST" and path == "/api/upload":
            state["seq"] += 1
            n = state["seq"]
            raw = request.content.decode("utf-8", "ignore")
            url = f"/uploads/file-{n}.webp"
            if 'name="field"' in raw and "mainImage" in raw:
                state["main_image"] = {
                    "id": 900 + n,
                    "documentId": f"media-{n}",
                    "url": url,
                }
            # STANDARD Strapi upload controller response: top-level array, 201.
            return httpx.Response(
                201,
                json=[
                    {
                        "id": 900 + n,
                        "documentId": f"media-{n}",
                        "url": url,
                        "alternativeText": "alt",
                    }
                ],
            )
        if method == "PUT" and path.startswith("/api/blogs/"):
            state["doc"].update(json.loads(request.content)["data"])
            return httpx.Response(200, json={"data": _flat()})
        if method == "GET" and path.startswith("/api/blogs/"):
            return httpx.Response(200, json={"data": _flat()})
        return httpx.Response(404, json={"error": "not found"})

    settings = _settings(tmp_path)
    provider = StrapiCMSProvider(
        settings=settings,
        client=httpx.AsyncClient(
            base_url=BASE, transport=httpx.MockTransport(handler)
        ),
        backoff_seconds=(0.001, 0.001, 0.001),
    )
    job = _job(db)
    row = await run_strapi_sync(db, job, provider, settings=settings)
    await provider.aclose()

    assert row.sync_status == StrapiSyncStatus.DRAFT_CREATED.value
    assert row.strapi_document_id == "doc-42"
    assert job.status == JobStatus.STRAPI_DRAFT_CREATED.value
    # hero + both inline uploads parsed from the top-level arrays
    rows = {r.filename: r for r in _image_rows(db)}
    assert rows["hero.webp"].strapi_url == BASE + "/uploads/file-1.webp"
    assert rows["hero.webp"].strapi_media_id == 901
    assert rows["inline-1.webp"].strapi_url == BASE + "/uploads/file-2.webp"
    assert rows["inline-2.webp"].strapi_url == BASE + "/uploads/file-3.webp"
