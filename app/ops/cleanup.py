"""Job cleanup (spec P9 "Cleanup").

Deletes **terminal jobs past the retention window** — their full row set
across every pipeline table plus their local image directory
(``{data_dir}/articles/{job_id}/``).

Rules (kept deliberately conservative):

* Only ``failed`` / ``cancelled`` jobs are ever auto-deletable. Success
  jobs (``ready`` / ``strapi_draft_created``) are NEVER auto-swept; the
  operator can pass ``--job-id`` to explicitly remove any terminal job
  (including success ones) regardless of retention.
* The retention window is keyed on ``completed_at`` (set by every failure
  path and by cancel). ``JOB_RETENTION_DAYS=0`` keeps everything.
* ``source_pages`` rows are NEVER deleted: they are a TTL cache shared
  across jobs (spec section 14.2) — expired entries simply miss
  ``find_fresh`` and get re-extracted on demand.
* ``--dry-run`` (default) reports what would be deleted and commits
  nothing.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.base import Base  # noqa: F401
from app.db import models  # noqa: F401 - register all models on Base
from app.db.models.article import ArticleReviewRow, ArticleVersionRow
from app.db.models.cost import ProviderCostEventRow
from app.db.models.images import ImageRow
from app.db.models.job import GenerationJob
from app.db.models.llm_usage import LLMUsageRow
from app.db.models.research import (
    ArticleOutlineRow,
    CompetitorAnalysisRow,
    ContentBriefRow,
    EvidenceNoteRow,
    SerpSynthesisRow,
)
from app.db.models.serp import SerpResult, SerpRun
from app.db.models.source import JobSource, SourcePage  # noqa: F401
from app.db.models.strapi_syncs import StrapiSyncRow
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

# Job-level deletion order: children before parents (FK-safe, cross-dialect).
_JOB_CHILD_TABLES: tuple[type, ...] = (
    ArticleReviewRow,   # -> article_versions, generation_jobs
    ArticleVersionRow,  # -> generation_jobs
    SerpResult,         # -> serp_runs
    SerpRun,            # -> generation_jobs
    JobSource,          # -> generation_jobs, source_pages (pages kept)
    LLMUsageRow,        # -> generation_jobs
    ProviderCostEventRow,  # -> generation_jobs (R-M02 ledger)
    StrapiSyncRow,      # -> generation_jobs
    ImageRow,           # -> generation_jobs
    CompetitorAnalysisRow,
    SerpSynthesisRow,
    EvidenceNoteRow,
    ContentBriefRow,
    ArticleOutlineRow,
)

# Auto path: only failed/cancelled jobs are ever swept.
AUTO_DELETABLE_STATUSES = ("failed", "cancelled")
# Explicit ``--job-id``: any terminal job (incl. success) may be removed.
TERMINAL_STATUSES = ("ready", "failed", "cancelled")


@dataclass
class CleanupSummary:
    """Result of one cleanup pass."""

    apply: bool = False
    jobs: list[dict] = field(default_factory=list)
    rows_deleted: dict[str, int] = field(default_factory=dict)
    dirs_removed: list[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(self.rows_deleted.values())


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def find_deletable_jobs(
    session: Session,
    *,
    retention_days: int,
    now: datetime | None = None,
    job_id: uuid.UUID | str | None = None,
) -> list[GenerationJob]:
    """Terminal jobs eligible for deletion.

    With ``job_id``: that one job, only if it is terminal (any retention).
    Otherwise: ``failed``/``cancelled`` jobs whose ``completed_at`` is
    older than the retention window.
    """
    now = _aware(now) or datetime.now(timezone.utc)
    stmt = select(GenerationJob)
    if job_id is not None:
        # Explicit target: any terminal job (incl. success), any age.
        stmt = stmt.where(
            GenerationJob.id == uuid.UUID(str(job_id)),
            GenerationJob.status.in_(TERMINAL_STATUSES),
        )
    else:
        stmt = stmt.where(GenerationJob.status.in_(AUTO_DELETABLE_STATUSES))
        if retention_days > 0:
            cutoff = now - timedelta(days=retention_days)
            stmt = stmt.where(
                GenerationJob.completed_at.is_not(None),
                GenerationJob.completed_at < cutoff,
            )
        else:
            return []
    rows = list(session.scalars(stmt.order_by(GenerationJob.created_at)))
    return rows


def delete_job_rows(session: Session, job: GenerationJob) -> dict[str, int]:
    """Delete every pipeline row owned by ``job`` (+ the job row itself).

    ``source_pages`` rows are left in place (shared TTL cache). Returns
    per-table delete counts.
    """
    job_id = job.id
    counts: dict[str, int] = {}
    for table in _JOB_CHILD_TABLES:
        if table is SerpResult:
            # serp_results key on serp_run_id (no direct job_id column).
            result = session.execute(
                delete(SerpResult).where(
                    SerpResult.serp_run_id.in_(
                        select(SerpRun.id).where(SerpRun.job_id == job_id)
                    )
                )
            )
        else:
            result = session.execute(delete(table).where(table.job_id == job_id))
        counts[table.__tablename__] = result.rowcount or 0
    result = session.execute(
        delete(GenerationJob).where(GenerationJob.id == job_id)
    )
    counts["generation_jobs"] = result.rowcount or 0
    session.commit()
    return counts


def count_job_rows(session: Session, job: GenerationJob) -> dict[str, int]:
    """Read-only per-table row counts owned by ``job`` (dry-run preview)."""
    job_id = job.id
    counts: dict[str, int] = {}
    for table in _JOB_CHILD_TABLES:
        if table is SerpResult:
            subq = select(SerpRun.id).where(SerpRun.job_id == job_id)
            stmt = select(func.count()).select_from(SerpResult).where(
                SerpResult.serp_run_id.in_(subq)
            )
        else:
            stmt = select(func.count()).select_from(table).where(
                table.job_id == job_id
            )
        counts[table.__tablename__] = session.scalar(stmt) or 0
    counts["generation_jobs"] = 1  # the job row itself
    return counts


def job_artifact_dir(job_id: uuid.UUID, settings: Settings) -> Path:
    """``{data_dir}/articles/{job_id}`` — the job's local artifact dir."""
    return Path(settings.data_dir) / "articles" / str(job_id)


def run_cleanup(
    session: Session,
    *,
    settings: Settings | None = None,
    retention_days: int | None = None,
    apply: bool = False,
    now: datetime | None = None,
    job_id: uuid.UUID | str | None = None,
) -> CleanupSummary:
    """One cleanup pass. Commits per job; never raises for missing dirs."""
    settings = settings or get_settings()
    retention_days = (
        settings.job_retention_days if retention_days is None else retention_days
    )
    summary = CleanupSummary(apply=apply)
    for job in find_deletable_jobs(
        session,
        retention_days=retention_days,
        now=now,
        job_id=job_id,
    ):
        info = {
            "job_id": str(job.id),
            "keyword": job.keyword,
            "status": job.status,
            "completed_at": _aware(job.completed_at),
        }
        if apply:
            counts = delete_job_rows(session, job)
            summary.rows_deleted.update(
                {k: summary.rows_deleted.get(k, 0) + v for k, v in counts.items()}
            )
            art = job_artifact_dir(job.id, settings)
            if art.exists():
                shutil.rmtree(art)
                summary.dirs_removed.append(str(art))
            info["rows_deleted"] = counts
        else:
            # Dry-run: report the projected row impact without committing.
            info["rows_would_delete"] = count_job_rows(session, job)
        summary.jobs.append(info)
        logger.info(
            "cleanup_job",
            extra={
                "event": "cleanup_job",
                "job_id": str(job.id),
                "status": job.status,
                "apply": apply,
            },
        )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.ops.cleanup",
        description="Delete expired terminal jobs (DB rows + image dirs).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete (default: dry-run, nothing is committed)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="override JOB_RETENTION_DAYS for this run",
    )
    parser.add_argument(
        "--job-id",
        type=str,
        default=None,
        help="delete one specific terminal job regardless of retention",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    import logging as _logging

    args = build_parser().parse_args(argv)
    _logging.basicConfig(level=_logging.INFO, format="%(message)s")
    with SessionLocal() as session:
        summary = run_cleanup(
            session,
            apply=args.apply,
            retention_days=args.days,
            job_id=args.job_id,
        )
    mode = "APPLY" if summary.apply else "DRY-RUN"
    print(f"[cleanup] mode={mode} jobs={len(summary.jobs)} "
          f"rows={summary.total_rows} dirs={len(summary.dirs_removed)}")
    for info in summary.jobs:
        line = (f"[cleanup] job={info['job_id']} status={info['status']} "
                f"keyword={info['keyword']!r} completed_at={info['completed_at']}")
        if summary.apply and info.get("rows_deleted"):
            line += f" rows={info['rows_deleted']}"
        elif info.get("rows_would_delete"):
            line += f" would_delete={sum(info['rows_would_delete'].values())} rows"
        print(line)
    for d in summary.dirs_removed:
        print(f"[cleanup] removed dir {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
