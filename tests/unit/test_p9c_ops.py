"""P9-C unit: operations tooling — Cleanup + Backup (spec P9 list).

Covers:

* ``cleanup`` — only terminal jobs past the retention window are
  deletable; success jobs, fresh failures and non-terminal jobs are
  never touched; ``retention_days=0`` keeps everything; ``job_id=``
  forces a single terminal job regardless of age but refuses
  non-terminal jobs; deletion removes every job-owned row (including
  ``serp_results`` via their ``serp_run_id``) but NEVER shared
  ``source_pages``; dry-run commits nothing; the job image directory
  is removed on apply only.
* ``backup`` — archive layout (``db/*.jsonl`` + ``db/manifest.json`` +
  ``artifacts.tar``); JSONL round-trips UUID/datetime/JSONB/FK-link
  cells (incl. unicode text) into a fresh database in FK order; restore
  is
  idempotent (re-restore does not duplicate); ``clear=True`` removes
  rows absent from the archive; prune keeps the newest N archives.
"""

from __future__ import annotations

import datetime
import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models  # noqa: F401 - register all models on Base
from app.db.base import Base
from app.db.models.images import ImageRow
from app.db.models.job import GenerationJob
from app.db.models.llm_usage import LLMUsageRow
from app.db.models.research import EvidenceNoteRow
from app.db.models.serp import SerpResult, SerpRun
from app.db.models.source import JobSource, SourcePage
from app.ops import backup, cleanup

NOW = datetime.datetime.now(datetime.timezone.utc)
DAY = datetime.timedelta(days=1)


# ------------------------------------------------------------- helpers
@pytest.fixture()
def db():
    """In-memory SQLite with all tables; returns (session_factory, engine)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return factory, engine


def fresh_sqlite():
    """A brand-new empty SQLite database (for restore targets)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def fake_settings(base: Path, *, retention=30, keep=3):
    class S:
        data_dir = str(base)
        backup_dir = str(base / "backups")
        job_retention_days = retention
        backup_keep = keep

    return S()


def _job(session, *, keyword, status, completed_at=None, **kw) -> GenerationJob:
    job = GenerationJob(keyword=keyword, status=status, **kw)
    job.started_at = NOW
    if completed_at is not None:
        job.completed_at = completed_at
    session.add(job)
    session.flush()
    return job


def _full_job_chain(session, job: GenerationJob) -> None:
    """One row in every job-owned table (incl. serp_results)."""
    run = SerpRun(
        job_id=job.id, provider="dataforseo", query=job.keyword,
        location_code=2840, language_code="en", device="desktop",
        raw_response={"organic": [1, 2]},
    )
    session.add(run)
    session.flush()
    session.add(SerpResult(
        serp_run_id=run.id, result_type="organic", rank=1,
        title="t", raw_item={"k": ["v", 3]},
    ))
    session.add(EvidenceNoteRow(
        job_id=job.id, claim="c", source_title="st",
        source_url="https://x.com", source_type="web",
        confidence="high", usage="supporting",
    ))
    session.add(LLMUsageRow(
        job_id=job.id, step="outline", model="m",
        input_tokens=1, output_tokens=2, duration_ms=7,
    ))


def _shared_page(session, job: GenerationJob) -> SourcePage:
    page = SourcePage(
        url="https://shared.com", normalized_url="https://shared.com",
        url_hash="s" * 64, content_markdown="c", content_hash="c" * 64,
        extractor="exa", first_seen_at=NOW, last_fetched_at=NOW,
    )
    session.add(page)
    session.flush()
    session.add(JobSource(job_id=job.id, source_page_id=page.id))
    session.flush()
    return page


def _image(session, job: GenerationJob, *, local_path: str) -> "ImageRow":
    row = ImageRow(
        job_id=job.id,
        role="hero",
        sort_order=1,
        purpose="hero",
        prompt="p",
        filename="a.webp",
        alt_text="alt",
        aspect_ratio="16:9",
        provider="openai",
        local_path=local_path,
    )
    session.add(row)
    session.flush()
    return row


# ------------------------------------------------------------ cleanup
def test_cleanup_dry_run_commits_nothing(db, tmp_path):
    session_factory, _ = db
    with session_factory() as session:
        job = _job(session, keyword="old", status="failed",
                   completed_at=NOW - 40 * DAY)
        _full_job_chain(session, job)
        session.commit()
        job_id = job.id

    with session_factory() as session:
        summary = cleanup.run_cleanup(
            session, settings=fake_settings(tmp_path),
            retention_days=30, apply=False,
        )
    assert [j["job_id"] for j in summary.jobs] == [str(job_id)]
    # Dry-run reports the candidate job but deletes nothing.
    assert summary.apply is False
    assert summary.total_rows == 0
    assert summary.rows_deleted == {} and summary.dirs_removed == []
    # ...but it previews the projected row impact (read-only, no commit).
    would = summary.jobs[0]["rows_would_delete"]
    assert would["generation_jobs"] == 1
    assert would["serp_results"] == 1
    assert would["serp_runs"] == 1
    assert would["evidence_notes"] == 1
    # Dry-run must not have committed: every row is still present.
    with session_factory() as session:
        assert session.get(GenerationJob, job_id) is not None
        assert session.scalar(select(func.count()).select_from(SerpResult)) == 1
        assert session.scalar(select(func.count()).select_from(SerpRun)) == 1


def test_cleanup_skips_success_fresh_and_non_terminal(db, tmp_path):
    session_factory, _ = db
    with session_factory() as session:
        _job(session, keyword="ready-old", status="ready",
             completed_at=NOW - 40 * DAY)
        _job(session, keyword="failed-fresh", status="failed", completed_at=NOW)
        # M01: a Strapi-sync-failed job (section 64) is in the unified
        # persisted state — non-terminal, WITH a completed_at, and
        # deliberately NOT in AUTO_DELETABLE_STATUSES: it holds the
        # documentId anchor and must never be auto-swept.
        _job(session, keyword="strapi-sync-failed",
             status="strapi_sync_failed", completed_at=NOW - 40 * DAY)
        _job(session, keyword="no-completed-at", status="failed")
        session.commit()

    with session_factory() as session:
        summary = cleanup.run_cleanup(
            session, settings=fake_settings(tmp_path),
            retention_days=30, apply=False,
        )
    # ready = success (never auto-deleted); fresh = not expired;
    # strapi_sync_failed is NOT in AUTO_DELETABLE_STATUSES (even with a
    # stale completed_at); no completed_at = window cannot be evaluated.
    assert summary.jobs == []


def test_cleanup_retention_zero_keeps_everything(db, tmp_path):
    session_factory, _ = db
    with session_factory() as session:
        _job(session, keyword="ancient", status="cancelled",
             completed_at=NOW - 400 * DAY)
        session.commit()
    with session_factory() as session:
        summary = cleanup.run_cleanup(
            session, settings=fake_settings(tmp_path, retention=0),
            retention_days=0, apply=True,
        )
    assert summary.jobs == []


def test_cleanup_apply_deletes_full_chain_keeps_shared_pages(db, tmp_path):
    session_factory, _ = db
    with session_factory() as session:
        old = _job(session, keyword="old", status="failed",
                   completed_at=NOW - 40 * DAY)
        keep = _job(session, keyword="keep", status="ready",
                    completed_at=NOW - 40 * DAY)
        _full_job_chain(session, old)
        page = _shared_page(session, old)
        session.add(JobSource(job_id=keep.id, source_page_id=page.id))
        session.commit()
        old_id = old.id
        keep_id = keep.id

    art = Path(tmp_path) / "data" / "articles" / str(old_id)
    (art / "images").mkdir(parents=True)
    (art / "images" / "hero.webp").write_bytes(b"img")
    settings = fake_settings(Path(tmp_path) / "data")

    with session_factory() as session:
        summary = cleanup.run_cleanup(
            session, settings=settings, retention_days=30, apply=True,
        )

    assert [j["keyword"] for j in summary.jobs] == ["old"]
    assert summary.rows_deleted["generation_jobs"] == 1
    assert summary.rows_deleted["serp_results"] == 1
    assert summary.rows_deleted["serp_runs"] == 1
    assert summary.rows_deleted["evidence_notes"] == 1
    assert summary.rows_deleted["llm_usage"] == 1
    assert summary.rows_deleted["job_sources"] == 1
    assert summary.dirs_removed == [str(art)]
    assert not art.exists()
    with session_factory() as session:
        assert session.get(GenerationJob, old_id) is None
        assert session.get(GenerationJob, keep_id) is not None
        # The shared source page survives; only the old job's link died.
        assert session.get(SourcePage, page.id) is not None
        assert session.scalar(select(func.count()).select_from(JobSource)) == 1
        assert session.scalar(select(func.count()).select_from(SerpRun)) == 0


def test_cleanup_explicit_job_id_bypasses_retention(db, tmp_path):
    session_factory, _ = db
    with session_factory() as session:
        job = _job(session, keyword="fresh", status="failed", completed_at=NOW)
        _full_job_chain(session, job)
        session.commit()
        job_id = job.id

    with session_factory() as session:
        summary = cleanup.run_cleanup(
            session, settings=fake_settings(tmp_path),
            retention_days=30, apply=True, job_id=job_id,
        )
    assert [j["keyword"] for j in summary.jobs] == ["fresh"]
    with session_factory() as session:
        assert session.get(GenerationJob, job_id) is None


def test_cleanup_explicit_job_id_allows_terminal_success(db, tmp_path):
    """Explicit --job-id may target a terminal SUCCESS job (operator override)."""
    session_factory, _ = db
    with session_factory() as session:
        job = _job(session, keyword="success", status="ready",
                   completed_at=NOW - 40 * DAY)
        _full_job_chain(session, job)
        session.commit()
        job_id = job.id

    with session_factory() as session:
        summary = cleanup.run_cleanup(
            session, settings=fake_settings(tmp_path),
            retention_days=30, apply=True, job_id=job_id,
        )
    assert [j["keyword"] for j in summary.jobs] == ["success"]
    with session_factory() as session:
        assert session.get(GenerationJob, job_id) is None


def test_cleanup_refuses_non_terminal_job_id(db, tmp_path):
    """A still-running (non-terminal) job is never deletable, even by id."""
    session_factory, _ = db
    with session_factory() as session:
        job = _job(session, keyword="running", status="strapi_syncing")
        session.commit()
        job_id = job.id
    with session_factory() as session:
        summary = cleanup.run_cleanup(
            session, settings=fake_settings(tmp_path),
            retention_days=30, apply=True, job_id=job_id,
        )
    assert summary.jobs == []
    with session_factory() as session:
        assert session.get(GenerationJob, job_id) is not None


# ------------------------------------------------------------ backup
def test_backup_archive_layout(db, tmp_path):
    session_factory, engine = db
    data = Path(tmp_path) / "data"
    with session_factory() as session:
        job = _job(session, keyword="kb", status="ready", completed_at=NOW)
        _full_job_chain(session, job)
        _shared_page(session, job)
        session.commit()
        job_id = job.id

    # A generated artifact to be packed.
    img = data / "articles" / str(job_id) / "images"
    img.mkdir(parents=True)
    (img / "a.webp").write_bytes(b"\xff\xd8fake")

    arch = backup.create_backup(fake_settings(data), engine=engine)

    assert arch.exists()
    assert arch.name.startswith("seo_backup_") and arch.name.endswith(".tar.gz")

    with tarfile.open(arch, "r:gz") as tar:
        names = {m.name for m in tar.getmembers() if m.isfile()}
        assert "db/manifest.json" in names
        assert "db/generation_jobs.jsonl" in names
        assert "db/serp_results.jsonl" in names
        assert "artifacts.tar" in names
        fh = tar.extractfile("db/manifest.json")
        assert fh is not None
        manifest = json.load(fh)
    assert manifest["app"] == "seo-autogen"
    assert manifest["tables"]["generation_jobs"] == 1
    assert manifest["tables"]["serp_results"] == 1
    assert manifest["tables"]["source_pages"] == 1
    assert manifest["tables"]["keyword_clusters"] == 0
    assert isinstance(manifest["created_at"], str)

    # The inner artifacts.tar holds the image under arcname "articles".
    with tarfile.open(arch, "r:gz") as tar:
        fh = tar.extractfile("artifacts.tar")
        assert fh is not None
        inner = tarfile.open(fileobj=io.BytesIO(fh.read()))
        inner_names = inner.getnames()
    assert any(n.endswith("a.webp") for n in inner_names)


def test_backup_restore_round_trip(db, tmp_path):
    session_factory, engine = db
    data = Path(tmp_path) / "data"
    with session_factory() as session:
        job = _job(session, keyword="rt", status="failed",
                   completed_at=NOW, error_code="UNEXPECTED",
                   error_message="msg with 中文")
        _full_job_chain(session, job)
        _shared_page(session, job)
        session.commit()
        job_id = job.id

    arch = backup.create_backup(fake_settings(data), engine=engine)

    target, t_session = fresh_sqlite()
    counts = backup.restore_archive(arch, target)["tables"]
    assert counts["generation_jobs"] == 1
    assert counts["serp_results"] == 1
    assert counts["serp_runs"] == 1
    assert counts["evidence_notes"] == 1
    assert counts["llm_usage"] == 1
    assert counts["source_pages"] == 1
    assert counts["job_sources"] == 1

    with t_session() as s:
        rjob = s.get(GenerationJob, job_id)
        assert rjob is not None
        assert rjob.status == "failed" and rjob.error_code == "UNEXPECTED"
        assert rjob.error_message == "msg with 中文"
        assert isinstance(rjob.completed_at, datetime.datetime)
        trun = s.scalars(select(SerpRun).where(SerpRun.job_id == job_id)).first()
        assert trun is not None
        results = s.scalars(
            select(SerpResult).where(SerpResult.serp_run_id == trun.id)).all()
        assert len(results) == 1
        assert results[0].raw_item == {"k": ["v", 3]}
        note = s.scalars(
            select(EvidenceNoteRow).where(EvidenceNoteRow.job_id == job_id)).first()
        assert note.confidence == "high" and note.source_url == "https://x.com"
        usage = s.scalars(
            select(LLMUsageRow).where(LLMUsageRow.job_id == job_id)).first()
        assert usage.duration_ms == 7 and usage.model == "m"
        # FK links survived the round-trip.
        link = s.scalars(select(JobSource).where(JobSource.job_id == job_id)).first()
        assert link is not None
        assert s.get(SourcePage, link.source_page_id) is not None

    # Idempotent: restoring again must not duplicate any row.
    backup.restore_archive(arch, target)
    with t_session() as s:
        assert s.scalar(select(func.count()).select_from(GenerationJob)) == 1
        assert s.scalar(select(func.count()).select_from(SerpResult)) == 1
        assert s.scalar(select(func.count()).select_from(SourcePage)) == 1


def test_backup_restore_clear_removes_stray_rows(db, tmp_path):
    session_factory, engine = db
    data = Path(tmp_path) / "data"
    with session_factory() as session:
        job = _job(session, keyword="c1", status="ready", completed_at=NOW)
        session.commit()
        job_id = job.id
    arch = backup.create_backup(fake_settings(data), engine=engine)

    target, t_session = fresh_sqlite()
    with t_session() as s:
        s.add(GenerationJob(keyword="stray", status="ready"))
        s.commit()

    counts = backup.restore_archive(arch, target, clear=True)["tables"]
    assert counts["generation_jobs"] == 1
    with t_session() as s:
        keywords = {r.keyword for r in s.scalars(select(GenerationJob))}
    assert keywords == {"c1"}


def test_backup_restore_replaces_changed_row(db, tmp_path):
    """Restore without --clear replaces an existing row with the same PK."""
    session_factory, engine = db
    data = Path(tmp_path) / "data"
    with session_factory() as session:
        job = _job(session, keyword="v1", status="ready", completed_at=NOW)
        session.commit()
        job_id = job.id
    arch = backup.create_backup(fake_settings(data), engine=engine)

    target, t_session = fresh_sqlite()
    with t_session() as s:
        s.add(GenerationJob(id=job_id, keyword="stale", status="failed"))
        s.commit()

    backup.restore_archive(arch, target)
    with t_session() as s:
        rows = s.scalars(select(GenerationJob)).all()
        assert len(rows) == 1
        assert rows[0].keyword == "v1"


# ------------------------------------------------------------ H12 (image restore)
def test_backup_archive_with_image_layout(db, tmp_path):
    """H12: a job with a generated image packs the image into artifacts.tar."""
    session_factory, engine = db
    data = Path(tmp_path) / "data"
    with session_factory() as session:
        job = _job(session, keyword="img", status="ready", completed_at=NOW)
        _full_job_chain(session, job)
        session.commit()
        job_id = job.id
    img_dir = data / "articles" / str(job_id) / "images"
    img_dir.mkdir(parents=True)
    (img_dir / "a.webp").write_bytes(b"\xff\xd8fake-bytes")
    with session_factory() as session:
        _image(session, session.get(GenerationJob, job_id),
               local_path=str(img_dir / "a.webp"))
        session.commit()

    arch = backup.create_backup(fake_settings(data), engine=engine)
    # validate_archive must now see both the image rows and the artifact files.
    info = backup.validate_archive(arch)
    assert info["artifact_files"]
    assert any(n.endswith("a.webp") for n in info["artifact_files"])


def test_backup_restore_recovers_images_into_fresh_dir(db, tmp_path):
    """H12 core: backup → delete source → restore into a fresh DATA_DIR +
    empty DB. The image file must exist at the new location and the restored
    DB row's local_path must point at it (path mapping + file check)."""
    session_factory, engine = db
    src_data = Path(tmp_path) / "src"
    with session_factory() as session:
        job = _job(session, keyword="rec", status="ready", completed_at=NOW)
        _full_job_chain(session, job)
        session.commit()
        job_id = job.id
    img_dir = src_data / "articles" / str(job_id) / "images"
    img_dir.mkdir(parents=True)
    (img_dir / "a.webp").write_bytes(b"\xff\xd8hero-image-bytes")
    with session_factory() as session:
        _image(session, session.get(GenerationJob, job_id),
               local_path=str(img_dir / "a.webp"))
        session.commit()

    arch = backup.create_backup(fake_settings(src_data), engine=engine)

    # Preserve the archive OUTSIDE the source dir, then simulate the loss:
    # delete the whole source data dir (DB rows + image files are gone).
    saved_arch = tmp_path / arch.name
    shutil.move(str(arch), str(saved_arch))
    shutil.rmtree(src_data)

    # Restore into a brand-new, separate data dir + empty DB.
    dst_data = Path(tmp_path) / "dst"
    target, t_session = fresh_sqlite()
    result = backup.restore_archive(saved_arch, target, data_dir=dst_data)

    # The image file was written into the new data dir...
    restored_file = dst_data / "articles" / str(job_id) / "images" / "a.webp"
    assert restored_file.is_file()
    assert restored_file.read_bytes() == b"\xff\xd8hero-image-bytes"
    # ...and the digest was reported.
    assert result["image_files"] and all(result["image_files"])
    assert list(result["image_files"]) == [str(restored_file)]
    # The restored DB row's local_path was re-anchored onto the new data dir.
    with t_session() as s:
        img = s.scalars(select(ImageRow)).first()
        assert img is not None
        assert img.local_path == str(restored_file)
        assert Path(img.local_path).is_file()


def test_backup_restore_out_of_sync_fails_without_db_change(db, tmp_path):
    """H12: an image row whose file is absent from the artifact tree (e.g. it
    was deleted after the DB was written) must fail the restore BEFORE the DB
    is touched — no half-restore, no dangling local_path."""
    session_factory, engine = db
    src_data = Path(tmp_path) / "src"
    with session_factory() as session:
        job = _job(session, keyword="oos", status="ready", completed_at=NOW)
        session.commit()
        job_id = job.id
    # A real file keeps the artifact tar non-empty, but the image ROW points
    # at a file that does not exist → the archive is out of sync with its DB.
    img_dir = src_data / "articles" / str(job_id) / "images"
    img_dir.mkdir(parents=True)
    (img_dir / "real.webp").write_bytes(b"decoy")
    with session_factory() as session:
        _image(session, session.get(GenerationJob, job_id),
               local_path=str(img_dir / "missing.webp"))
        session.commit()
    arch = backup.create_backup(fake_settings(src_data), engine=engine)

    dst_data = Path(tmp_path) / "dst"
    target, t_session = fresh_sqlite()
    with pytest.raises(backup.BackupError):
        backup.restore_archive(arch, target, data_dir=dst_data)
    # No half-restore: the target DB was left untouched.
    with t_session() as s:
        assert s.scalar(select(func.count()).select_from(GenerationJob)) == 0
        assert s.scalar(select(func.count()).select_from(ImageRow)) == 0


def test_backup_validate_rejects_truncated_archive(db, tmp_path):
    """H12: a corrupt/truncated archive must be rejected up-front (H12)."""
    session_factory, engine = db
    data = Path(tmp_path) / "data"
    with session_factory() as session:
        _job(session, keyword="trunc", status="ready", completed_at=NOW)
        session.commit()
    arch = backup.create_backup(fake_settings(data), engine=engine)
    raw = arch.read_bytes()
    truncated = tmp_path / "truncated.tar.gz"
    truncated.write_bytes(raw[: len(raw) // 2])  # chop the gzip in half
    with pytest.raises(backup.BackupError):
        backup.validate_archive(truncated)


def test_backup_validate_rejects_non_backup_file(tmp_path):
    """H12: a file that is not a backup at all must be rejected."""
    not_a_backup = tmp_path / "not_a_backup.tar.gz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("db/manifest.json")
        import os
        payload = b"not-json"
        info.size = len(payload)
        tar.addfile(info, buf and io.BytesIO(payload))
    not_a_backup.write_bytes(buf.getvalue())
    with pytest.raises(backup.BackupError):
        backup.validate_archive(not_a_backup)


def test_backup_prune_keeps_newest_n(db, tmp_path):
    session_factory, engine = db
    data = Path(tmp_path) / "data"
    settings = fake_settings(data, keep=2)
    with session_factory() as session:
        session.add(GenerationJob(keyword="a", status="ready"))
        session.commit()

    # Create 4 archives with distinct, ordered names.
    created = backup.create_backup(settings, engine=engine)
    shutil.copyfile(created, created.with_name("seo_backup_20260101T000001Z.tar.gz"))
    shutil.copyfile(created, created.with_name("seo_backup_20260101T000002Z.tar.gz"))
    shutil.copyfile(created, created.with_name("seo_backup_20260101T000003Z.tar.gz"))
    # ensure the original itself is the newest one (its real stamp sorts
    # after the 2026-01-01 copies for any realistic wall clock)
    assert len(backup.list_backups(settings)) == 4

    removed = backup.prune_backups(settings)
    assert len(removed) == 2
    remaining = backup.list_backups(settings)
    assert len(remaining) == 2
    # Oldest two removed, newest two kept.
    kept_names = {p.name for p in remaining}
    removed_names = {p.name for p in removed}
    assert "seo_backup_20260101T000001Z.tar.gz" in removed_names
    assert "seo_backup_20260101T000002Z.tar.gz" in removed_names
    assert "seo_backup_20260101T000003Z.tar.gz" in kept_names

    # Pruning again with nothing to prune removes nothing.
    assert backup.prune_backups(settings) == []
