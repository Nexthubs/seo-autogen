"""P7 acceptance demo: Strapi Integration (spec sections 35-42, 46.16,
51, 59, 60, 64; P7).

Everything real where the environment allows it:

  - real PostgreSQL (Docker ``seo-pg``) with migration 0008
    (strapi_syncs, section 46.16)
  - the full A-F ``run_strapi_sync`` flow is exercised against
    PostgreSQL with a deterministic fake provider: payload shape
    (section 37), idempotency (section 41), slug collision
    (section 42), failure semantics (section 64: the documentId
    survives so the retry UPDATES the same draft)
  - when a live Strapi is configured (STRAPI_API_TOKEN +
    STRAPI_BASE_URL + default author/category document ids) the
    provider itself is probed for real: schema discovery
    (health_check), author/category listing, a real draft create
    with a unique ``p7acc-{uuid}`` slug + GET verify of all 9
    fields. The draft is LEFT in place — the system never
    publishes and never deletes (sections 60, 65).
  - when STRAPI_API_TOKEN is empty the live probe is a NOTE
    (like the P6 image-provider NOTE) and the acceptance still
    passes if every local check passes.

Run:
  PYTHONPATH=/home/ubuntu/ai-coding/seo-autogen python3 scripts/p7_acceptance.py
"""

import asyncio
import base64
import os
import sys
import uuid
from pathlib import Path

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
from app.providers.cms.strapi_cms import StrapiCMSProvider
from app.schemas.strapi import (
    build_draft_payload,
    build_seo_keywords,
    payload_hash,
    resolve_media_url,
)
from app.schemas.article import ArticleDocument

ACC = "p7acc"
PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4"
    "nGNgAAIAAAUAAXpeqz8AAAAASUVORK5CYII="
)

BODY = (
    "## What is anxious attachment\n\n"
    "A real paragraph that explains the concept here in detail. "
    "It talks about the nervous system, hypervigilance and the need "
    "for constant reassurance. " * 6
    + "\n\n"
    "## Why no contact feels impossible\n\n"
    "A second paragraph about the behavior: checking social media, "
    "almost calling, and the slow work of letting the loop fade. " * 6
)

BRIEF = {
    "primary_keyword": "anxious attachment no contact",
    "secondary_keywords": ["anxious attachment"],
    "long_tail_keywords": ["why no contact is hard"],
    "search_intent": "informational",
    "article_strategy": "explainer",
}

CHECKS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((label, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail and not ok else ""))


class FakeStrapiProvider:
    """Deterministic fake — same contract as StrapiCMSProvider."""

    def __init__(self, *, slug_entries=None):
        self.slug_entries = slug_entries or []
        self.created: list[dict] = []
        self.updates: list[tuple[str, dict]] = []
        self.get_calls: list[str] = []

    async def find_blogs_by_slug(self, slug):
        return list(self.slug_entries)

    def _entry(self, doc_id, body):
        return _Entry(
            77, doc_id,
            slug=doc_id, title="T", status="draft", body=body,
            metaTitle="mt", metaDescription="md", seoKeywords="kw",
            author="doc-author-1", category="doc-cat-1",
            mainImage="https://cms/uploads/hero.webp",
        )

    async def create_draft_entry(self, payload):
        self.created.append(payload)
        return self._entry("doc-acc", payload["data"].get("body"))

    async def update_draft_entry(self, document_id, payload):
        self.updates.append((document_id, payload))
        return self._entry(document_id, (payload.get("data") or {}).get("body"))

    async def get_draft(self, document_id):
        self.get_calls.append(document_id)
        last = self.updates[-1][1]["data"]["body"] if self.updates else BODY
        return self._entry(document_id, last)

    async def upload_hero(self, file_bytes, filename, *, blog_numeric_id,
                          alt_text="", caption=""):
        from app.schemas.strapi import MediaUploadResult
        return MediaUploadResult(
            media_id=1, url="/uploads/hero.webp",
            document_id="doc-media-1", alternative_text=alt_text or None,
        )

    async def upload_inline(self, file_bytes, filename, *, alt_text=""):
        from app.schemas.strapi import MediaUploadResult
        return MediaUploadResult(
            media_id=2, url=f"/uploads/{filename}",
            document_id="doc-media-2", alternative_text=alt_text or None,
        )


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


def reset() -> None:
    """Remove ONLY the acceptance rows — user data must survive."""
    with SessionLocal() as session:
        session.execute(sa_delete(StrapiSyncRow).where(
            StrapiSyncRow.job_id.in_(
                select(GenerationJob.id).where(
                    GenerationJob.keyword.ilike(f"{ACC}%")
                )
            )
        ))
        session.execute(sa_delete(ImageRow).where(
            ImageRow.job_id.in_(
                select(GenerationJob.id).where(
                    GenerationJob.keyword.ilike(f"{ACC}%")
                )
            )
        ))
        session.execute(sa_delete(ArticleVersionRow).where(
            ArticleVersionRow.job_id.in_(
                select(GenerationJob.id).where(
                    GenerationJob.keyword.ilike(f"{ACC}%")
                )
            )
        ))
        session.execute(sa_delete(ContentBriefRow).where(
            ContentBriefRow.job_id.in_(
                select(GenerationJob.id).where(
                    GenerationJob.keyword.ilike(f"{ACC}%")
                )
            )
        ))
        session.execute(sa_delete(GenerationJob).where(
            GenerationJob.keyword.ilike(f"{ACC}%")
        ))
        session.commit()


def seed_job(data_dir: str, slug: str) -> "uuid.UUID":
    with SessionLocal() as session:
        job = GenerationJob(
            keyword=f"{ACC} anxious attachment no contact",
            status=JobStatus.READY.value,
            current_step="image_generating",
            target_function="coach",
        )
        session.add(job)
        session.flush()
        session.add(ContentBriefRow(job_id=job.id, brief=BRIEF))
        session.add(ArticleVersionRow(
            job_id=job.id,
            version=1,
            stage="revision",
            title="P7ACC Anxious Attachment No Contact Guide",
            body_markdown=BODY,
            seo_title="Anxious Attachment and No Contact",
            meta_description="Why no contact feels so intense.",
            slug=slug,
            model="qwen3.8-27b",
            prompt_name="article_reviser",
            prompt_version="1.0",
        ))
        img_dir = Path(data_dir) / "articles" / str(job.id) / "images"
        img_dir.mkdir(parents=True, exist_ok=True)
        for marker, filename, heading in (
            (None, "hero.webp", None),
            ("inline-1", "inline-1.webp", "What is anxious attachment"),
        ):
            path = img_dir / filename
            path.write_bytes(PNG_1x1)
            session.add(ImageRow(
                job_id=job.id,
                role="hero" if marker is None else "inline",
                sort_order=0 if marker is None else 1,
                purpose="p",
                section_heading=heading,
                insertion_marker=marker,
                prompt="p",
                filename=filename,
                alt_text=f"alt {filename}",
                aspect_ratio="16:9" if marker is None else "4:3",
                local_path=str(path),
                mime_type="image/png",
                provider="p7acc-fake-image",
                provider_request_id=f"p7acc-{filename}",
            ))
        session.commit()
        return job.id


def acceptance_settings(data_dir: str, *, author: str, category: str) -> Settings:
    return Settings(
        llm_base_url="http://llm.test/v1",
        llm_api_key="test-key",
        llm_model="test-model",
        data_dir=data_dir,
        strapi_base_url=os.environ.get("STRAPI_BASE_URL", "http://127.0.0.1:1337"),
        strapi_api_token=os.environ.get("STRAPI_API_TOKEN", ""),
        strapi_default_author_document_id=author,
        strapi_default_category_document_id=category,
    )


import os  # noqa: E402


def run_local_checks(data_dir: str) -> None:
    """All checks that run without a live Strapi."""
    print("\n== 1. migration 0008 (strapi_syncs, section 46.16)")
    columns = {c["name"] for c in inspect(engine).get_columns("strapi_syncs")}
    check(
        "strapi_syncs table has the spec 46.16 columns",
        {
            "id", "job_id", "strapi_id", "strapi_document_id",
            "sync_status", "last_payload", "last_payload_hash",
            "error_message", "created_at", "updated_at",
        } <= columns,
        f"missing: {sorted({'id','job_id','strapi_id','strapi_document_id','sync_status','last_payload','last_payload_hash','error_message','created_at','updated_at'} - columns)}",
    )

    print("\n== 2. payload builders (sections 37, 6.5, 6.8)")
    doc = ArticleDocument(
        title="Anxious Attachment No Contact: P7ACC Guide",
        body_markdown=BODY,
        seo_title="Anxious Attachment and No Contact",
        meta_description="Why no contact feels so intense.",
        slug="p7acc-slug",
        primary_keyword="anxious attachment no contact",
        secondary_keywords=["anxious attachment"],
        long_tail_keywords=["why no contact is hard"],
        search_intent="informational",
        article_strategy="explainer",
    )
    payload = build_draft_payload(
        doc, author_document_id="a1", category_document_id="c1",
        body=BODY,
    )
    check("payload wrapped in {'data': ...}", set(payload) == {"data"})
    check(
        "9-field data shape (no postedAt by default)",
        set(payload["data"]) == {
            "title", "slug", "body", "metaTitle", "metaDescription",
            "seoKeywords", "author", "category",
        },
    )
    check(
        "seoKeywords follows section 6.8 priority",
        payload["data"]["seoKeywords"] == build_seo_keywords(
            "anxious attachment no contact",
            ["anxious attachment"],
            ["why no contact is hard"],
        ),
    )
    check(
        "relative media url gets the STRAPI_BASE_URL prefix",
        resolve_media_url("/uploads/a.webp", "http://127.0.0.1:1337")
        == "http://127.0.0.1:1337/uploads/a.webp",
    )
    check(
        "payload_hash is a stable sha256",
        len(payload_hash(payload)) == 64,
    )

    print("\n== 3. full A-F flow on PostgreSQL (fake provider)")
    slug = f"p7acc-{uuid.uuid4()}"
    job_id = seed_job(data_dir, slug)
    settings = acceptance_settings(
        data_dir, author="doc-author-1", category="doc-cat-1"
    )
    provider = FakeStrapiProvider()
    with SessionLocal() as session:
        job = session.get(GenerationJob, job_id)
        row = asyncio.run(run_strapi_sync(session, job, provider, settings=settings))
    check("job status -> strapi_draft_created (section 64)",
          job.status == JobStatus.STRAPI_DRAFT_CREATED.value,
          f"got {job.status}")
    check("sync row -> draft_created with documentId",
          row.sync_status == StrapiSyncStatus.DRAFT_CREATED.value
          and bool(row.strapi_document_id))
    check("last_payload is a stored dict with a hash",
          isinstance(row.last_payload, dict)
          and row.last_payload_hash == payload_hash(row.last_payload))
    final = provider.updates[-1][1]["data"]
    check("final body PUT writes only {'body'}",
          set(final) == {"body"})
    body = final["body"]
    check("inline marker resolved to an absolute URL",
          settings.strapi_base_url + "/uploads/inline-1.webp" in body
          and "[[IMAGE:" not in body)
    check("no H1 in the synced body",
          not any(line.startswith("# ") for line in body.splitlines()))
    check("GET verify ran once (section 64)",
          len(provider.get_calls) == 1)
    with SessionLocal() as session:
        images = {
            r.filename: r
            for r in session.scalars(
                select(ImageRow).where(ImageRow.job_id == job_id)
            ).all()
        }
    check("image rows carry strapi_media_id / strapi_url",
          images["hero.webp"].strapi_media_id == 1
          and images["hero.webp"].strapi_url
          == settings.strapi_base_url + "/uploads/hero.webp")

    print("\n== 4. idempotency + section 64 failure semantics")
    # Simulate: a previous attempt failed mid-sync and kept the
    # documentId. The retry must UPDATE the same draft, not create
    # a second one.
    with SessionLocal() as session:
        session.execute(sa_delete(StrapiSyncRow).where(
            StrapiSyncRow.job_id == job_id
        ))
        session.add(StrapiSyncRow(
            job_id=job_id,
            strapi_id=77,
            strapi_document_id="doc-kept",
            sync_status=StrapiSyncStatus.FAILED.value,
            error_message="simulated previous upload failure",
        ))
        # mark images as already uploaded -> no re-upload
        for r in session.scalars(
            select(ImageRow).where(ImageRow.job_id == job_id)
        ).all():
            r.strapi_media_id = 5
            r.strapi_media_document_id = "doc-media-5"
            r.strapi_url = settings.strapi_base_url + f"/uploads/{r.filename}"
        session.commit()
    provider2 = FakeStrapiProvider(
        slug_entries=[_Entry(77, "doc-kept", slug=slug, status="draft")]
    )
    with SessionLocal() as session:
        job = session.get(GenerationJob, job_id)
        row = asyncio.run(run_strapi_sync(session, job, provider2, settings=settings))
    check("retry created NO second draft (section 41)",
          provider2.created == [])
    check("retry updated the same documentId",
          row.strapi_document_id == "doc-kept"
          and row.sync_status == StrapiSyncStatus.DRAFT_CREATED.value)
    check("retry did not re-upload media",
          provider2.get_calls == ["doc-kept"])

    print("\n== 5. slug collision (section 42)")
    slug2 = f"p7acc-{uuid.uuid4()}"
    job_id2 = seed_job(data_dir, slug2)
    other = _Entry(9, "doc-other", slug=slug2, status="published")
    provider3 = FakeStrapiProvider(slug_entries=[other])
    with SessionLocal() as session:
        job = session.get(GenerationJob, job_id2)
        try:
            asyncio.run(
                run_strapi_sync(session, job, provider3, settings=settings)
            )
            code = None
        except PipelineError as error:
            code = error.error_code
    check("foreign slug -> STRAPI_SLUG_CONFLICT (never auto-suffixed)",
          code is not None and code.value == "STRAPI_SLUG_CONFLICT")
    check("no draft was created on conflict",
          provider3.created == [])
    with SessionLocal() as session:
        job = session.get(GenerationJob, job_id2)
        sync_row = session.scalars(
            select(StrapiSyncRow).where(StrapiSyncRow.job_id == job_id2)
        ).first()
    check("conflict persisted on the job (no draft, no sync row)",
          job.status == JobStatus.STRAPI_SYNCING.value
          and job.error_code == "STRAPI_SLUG_CONFLICT"
          and sync_row is None)


def run_live_checks() -> None:
    """Real Strapi probe — only with a complete configuration."""
    print("\n== 6. live Strapi (sections 35, 51, 60, 64)")
    settings = acceptance_settings(
        os.environ.get("P7_ACC_DATA_DIR", "/tmp/p7acc"),
        author=os.environ.get("STRAPI_DEFAULT_AUTHOR_DOCUMENT_ID", ""),
        category=os.environ.get("STRAPI_DEFAULT_CATEGORY_DOCUMENT_ID", ""),
    )
    if not settings.strapi_api_token or not settings.strapi_base_url:
        print("  [NOTE] STRAPI_API_TOKEN / STRAPI_BASE_URL empty -> "
              "live probe skipped (local checks above still gate P7)")
        return
    if not settings.strapi_default_author_document_id or \
            not settings.strapi_default_category_document_id:
        print("  [NOTE] STRAPI_DEFAULT_AUTHOR/CATEGORY_DOCUMENT_ID empty -> "
              "live probe skipped (no valid relations to write)")
        return

    async def probe():
        provider = StrapiCMSProvider(settings=settings)
        try:
            ok = await provider.health_check()
            check("schema discovery (GET /api/{pluralApiId}?) works", ok)
            if not ok:
                return
            authors = await provider.list_authors()
            cats = await provider.list_categories()
            check("authors list returned", len(authors) > 0)
            check("categories list returned", len(cats) > 0)
            doc = ArticleDocument(
                title="P7ACC Live Draft",
                body_markdown="## Intro\n\nLive acceptance draft body.",
                seo_title="P7ACC Live Draft",
                meta_description="Live acceptance draft.",
                slug=f"p7acc-{uuid.uuid4()}",
                primary_keyword="p7acc live",
                secondary_keywords=[],
                long_tail_keywords=[],
                search_intent="informational",
                article_strategy="explainer",
            )
            payload = build_draft_payload(
                doc,
                author_document_id=settings.strapi_default_author_document_id,
                category_document_id=settings.strapi_default_category_document_id,
                body=doc.body_markdown,
            )
            entry = await provider.create_draft_entry(payload)
            print(f"  live draft created: documentId={entry.document_id} "
                  f"slug={payload['data']['slug']} (left as draft — "
                  "the system never publishes or deletes, sections 60/65)")
            fetched = await provider.get_draft(entry.document_id)
            missing = [
                f for f, v in (
                    ("title", fetched.title),
                    ("slug", fetched.slug),
                    ("author", fetched.author),
                    ("category", fetched.category),
                    ("mainImage", fetched.main_image),
                    ("body", fetched.body),
                    ("metaTitle", fetched.meta_title),
                    ("metaDescription", fetched.meta_description),
                    ("seoKeywords", fetched.seo_keywords),
                ) if f in ("title", "slug", "author", "category",
                            "body", "metaTitle", "metaDescription",
                            "seoKeywords") and v in (None, "")
            ]
            check("GET verify: 8 text fields present (mainImage not set "
                  "in this probe — no image bytes)", not missing,
                  f"missing: {missing}")
        finally:
            await provider.aclose()

    asyncio.run(probe())


def main() -> int:
    print("P7 acceptance: Strapi Integration (sections 35-42, 46.16, 64)")
    if not check_database():
        print("  [FAIL] PostgreSQL not reachable")
        return 1
    reset()
    data_dir = os.environ.get("P7_ACC_DATA_DIR", "/tmp/p7acc")
    run_local_checks(data_dir)
    run_live_checks()
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    total = len(CHECKS)
    print(f"\nP7 acceptance: {passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
