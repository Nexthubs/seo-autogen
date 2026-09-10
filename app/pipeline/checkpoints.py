"""Per-step pipeline checkpoints (P9-A — spec sections 9, 44, 51).

The 15 pipeline steps each commit a stable checkpoint row on success
(spec section 9: "save DB + structured result + status + commit + next").
This module turns those rows into the queryable map P9 needs:

- :func:`step_done` — did this step already commit its output?
- :func:`first_incomplete_step` — the resume-from-checkpoint boundary
  ("Resume from checkpoint": skip every done step, re-run from the first
  step that is not done).
- :func:`reset_from_step` — drop ONLY the *re-runnable* outputs of the given
  step and every later step ("Retry by step": a step re-run must start from a
  clean state for its own per-run artifacts, e.g. a fresh SERP run or a new
  competitor-analysis batch).

H11 — immutable article history: the article ``versions`` and ``reviews``
rows are an append-only, immutable log (spec sections 27-28, 46.13-46.14).
A retry re-runs the writer/reviser and APPENDS a new version rather than
deleting the old one, so :func:`reset_from_step` deliberately does NOT touch
``article_versions`` / ``article_reviews``. The "current valid" draft is
DERIVED from that history (the newest ``stage="writer"`` version; the newest
version overall is the final shipped one) — see the ``step_done`` notes below.

Other per-run rows (SERP, sources, research, outline, images, LLM usage) are
deleted explicitly (not left to ORM/FK cascades) so reset behaves identically
on PostgreSQL (``ondelete=CASCADE``) and SQLite (no foreign key pragma in unit
tests).
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

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
from app.db.models.serp import SerpResult, SerpRun
from app.db.models.source import JobSource

#: The 15 checkpoint steps in orchestrator order (spec section 9).
#: ``STEP_NAMES.index(name) + 1`` is the 1-based step index used by the
#: retry API and the worker options.
STEP_NAMES: tuple[str, ...] = (
    "keyword_prepare",
    "serp_search",
    "source_extract",
    "competitor_analysis",
    "serp_synthesis",
    "evidence_research",
    "content_brief",
    "outline",
    "article_writer",
    "seo_review",
    "fact_review",
    "style_review",
    "article_reviser",
    "image_plan",
    "image_generate",
)

#: Human labels for the per-step retry UI (spec 43.2 job detail actions).
STEP_LABELS: dict[str, str] = {
    "keyword_prepare": "Keyword preparation",
    "serp_search": "SERP search",
    "source_extract": "Source extraction",
    "competitor_analysis": "Competitor analysis",
    "serp_synthesis": "SERP synthesis",
    "evidence_research": "Evidence research",
    "content_brief": "Content brief",
    "outline": "Outline",
    "article_writer": "Article draft",
    "seo_review": "SEO review",
    "fact_review": "Fact review",
    "style_review": "Style review",
    "article_reviser": "Article revision",
    "image_plan": "Image plan",
    "image_generate": "Image generation",
}


def step_index(step_name: str) -> int:
    """1-based checkpoint index for a step name (raises on unknown)."""
    try:
        return STEP_NAMES.index(step_name) + 1
    except ValueError as exc:
        raise ValueError(f"unknown pipeline step: {step_name!r}") from exc


def _count(session: Session, model, *where) -> int:
    stmt = select(func.count()).select_from(model).where(*where)
    return int(session.scalar(stmt) or 0)


def _version_exists(session: Session, job: GenerationJob, stage: str) -> bool:
    return (
        _count(
            session,
            ArticleVersionRow,
            ArticleVersionRow.job_id == job.id,
            ArticleVersionRow.stage == stage,
        )
        > 0
    )


def _latest_version(session: Session, job: GenerationJob) -> ArticleVersionRow | None:
    return (
        session.scalars(
            select(ArticleVersionRow)
            .where(ArticleVersionRow.job_id == job.id)
            .order_by(ArticleVersionRow.version.desc())
        )
        .first()
    )


def _writer_version(session: Session, job: GenerationJob) -> ArticleVersionRow | None:
    return (
        session.scalars(
            select(ArticleVersionRow)
            .where(
                ArticleVersionRow.job_id == job.id,
                ArticleVersionRow.stage == "writer",
            )
            .order_by(ArticleVersionRow.version.desc())
        )
        .first()
    )


def _review_present(session: Session, version: ArticleVersionRow, review_type: str) -> bool:
    return (
        _count(
            session,
            ArticleReviewRow,
            ArticleReviewRow.article_version_id == version.id,
            ArticleReviewRow.review_type == review_type,
        )
        > 0
    )


def step_done(session: Session, job: GenerationJob, step_name: str) -> bool:
    """True when ``step_name`` has committed its checkpoint output.

    The predicates key on the rows the steps themselves persist — nothing
    here trusts in-memory state. Notes per step:

    - ``keyword_prepare`` never fails and writes no row of its own; its
      "done" signal is that the run has started at all (``started_at`` is
      set before step 1 runs, and is null on a queued, never-started job).
      ``keyword_metrics_available`` is a non-nullable bool, so it cannot
      distinguish "not yet run" from "run in SERP-only mode".
    - ``serp_search`` always appends a new run; any run means done.
    - ``source_extract`` checkpoints ``job_sources`` links; none means the
      step never finished (it raises ``SOURCE_EMPTY`` otherwise).
    - the three review steps attach one row per reviewed version; they are
      done when the review row exists on the *current writer draft* (newest
      ``stage="writer"`` version) that the reviews target. An older draft's
      reviews (kept as immutable history, H11) do not count.
    - ``article_reviser`` is done only when a ``revision``-stage version
      exists that is NEWER than the current writer draft: after a writer
      retry the draft is a new writer version, so any older (stale) revision
      no longer marks the step done.
    - ``image_plan`` is done when plan rows exist; ``image_generate`` is
      done only when EVERY plan row has a ``local_path`` (partial
      generation survives as a partial checkpoint by design, spec section
      9's retry example).
    """
    if step_name == "keyword_prepare":
        return job.started_at is not None
    if step_name == "serp_search":
        return _count(session, SerpRun, SerpRun.job_id == job.id) > 0
    if step_name == "source_extract":
        return _count(session, JobSource, JobSource.job_id == job.id) > 0
    if step_name == "competitor_analysis":
        # Done only when EVERY extracted source has an analysis. A run that
        # failed mid-step leaves a partial set (e.g. 3 of 5); treating that
        # as "done" would make resume skip the step and never analyse the
        # remaining sources, so the predicate must be all-or-nothing.
        sources = _count(session, JobSource, JobSource.job_id == job.id)
        return sources > 0 and (
            _count(
                session,
                CompetitorAnalysisRow,
                CompetitorAnalysisRow.job_id == job.id,
            )
            >= sources
        )
    if step_name == "serp_synthesis":
        return (
            _count(session, SerpSynthesisRow, SerpSynthesisRow.job_id == job.id) > 0
        )
    if step_name == "evidence_research":
        return (
            _count(session, EvidenceNoteRow, EvidenceNoteRow.job_id == job.id) > 0
        )
    if step_name == "content_brief":
        return (
            _count(session, ContentBriefRow, ContentBriefRow.job_id == job.id) > 0
        )
    if step_name == "outline":
        return (
            _count(
                session, ArticleOutlineRow, ArticleOutlineRow.job_id == job.id
            )
            > 0
        )
    if step_name == "article_writer":
        return _version_exists(session, job, "writer")
    if step_name in ("seo_review", "fact_review", "style_review"):
        # Reviews attach to the *writer* version the writer produced (the
        # reviser later writes a separate ``revision`` version, so the latest
        # version is the revision once it exists — key on the writer stage
        # directly so a fully-done job still reports its reviews as done).
        version = _writer_version(session, job)
        return version is not None and _review_present(
            session, version, step_name.split("_")[0]
        )
    if step_name == "article_reviser":
        # H11: a stale revision (older than the current writer draft, kept as
        # immutable history after a writer retry) does NOT mark this step
        # done — resume must re-revise from the fresh draft.
        writer = _writer_version(session, job)
        if writer is None:
            return False
        return _count(
            session,
            ArticleVersionRow,
            ArticleVersionRow.job_id == job.id,
            ArticleVersionRow.stage == "revision",
            ArticleVersionRow.version > writer.version,
        ) > 0
    if step_name == "image_plan":
        return _count(session, ImageRow, ImageRow.job_id == job.id) > 0
    if step_name == "image_generate":
        rows = (
            session.scalars(
                select(ImageRow).where(ImageRow.job_id == job.id)
            )
            .all()
        )
        return bool(rows) and all(row.local_path for row in rows)
    raise ValueError(f"unknown pipeline step: {step_name!r}")


def checkpoint_status(
    session: Session, job: GenerationJob
) -> list[dict]:
    """Per-step done flags for the job-detail retry UI (spec 43.2)."""
    return [
        {
            "name": name,
            "index": index,
            "label": STEP_LABELS[name],
            "done": step_done(session, job, name),
        }
        for index, name in enumerate(STEP_NAMES, start=1)
    ]


def first_incomplete_step(session: Session, job: GenerationJob) -> int | None:
    """1-based index of the first step without a checkpoint (or ``None``
    when the whole chain is done — a resume would be a no-op)."""
    for index, name in enumerate(STEP_NAMES, start=1):
        if not step_done(session, job, name):
            return index
    return None


def reset_from_step(
    session: Session, job: GenerationJob, step_index: int
) -> int:
    """Delete the checkpoint outputs of ``step_index`` and every later step.

    ``step_index=1`` is the full reset a fresh retry needs; a per-step
    retry (10-15) keeps every earlier checkpoint untouched. The step
    itself is NOT re-run here — the orchestrator runs it after this reset.
    Returns the number of steps covered (``15 - step_index + 1``).

    Deletion is scope-correct per the steps' idempotency model — only the
    per-run, re-runnable artifacts are dropped:

    - ``serp_search`` (2) adds a run whose results cascade — delete both
      explicitly.
    - ``source_extract`` (3) owns the ``job_sources`` links.
    - ``image_plan`` (14) owns the plan rows; ``image_generate`` (15) is
      partial-idempotent (per-image ``local_path`` overwrite) so a 15-reset
      deletes nothing.
    - ``llm_usage`` (P9-B1, spec section 54) is an IMMUTABLE, append-only
      cost/telemetry LEDGER: a re-run APPENDS new rows (the re-makes), it
      does NOT delete the earlier run's rows. Deleting them (the old
      behaviour) destroyed the cost history that section 54 asks us to keep
      "for later cost/performance analysis"; M12 requires retries to
      accumulate, so nothing about a step's usage rows is ever deleted here.

    The article ``versions`` and ``reviews`` (steps 9-13) are NOT deleted:
    they are immutable, append-only history (H11, spec sections 27-28,
    46.13-46.14). A retry re-runs the writer/reviser and appends a new
    version; the current draft/final is derived from the history, and old
    versions + reviews stay queryable for audit.
    """
    if not 1 <= step_index <= len(STEP_NAMES):
        raise ValueError(
            f"step_index must be 1..{len(STEP_NAMES)}, got {step_index}"
        )

    # keyword_prepare (1) checkpoints a job flag, not a row: restore it to
    # the model default (False) — its done-signal is started_at, which the
    # retry route clears when re-queueing.
    if step_index <= 1:
        job.keyword_metrics_available = False
    if step_index <= 2:
        session.execute(
            delete(SerpResult).where(
                SerpResult.serp_run_id.in_(
                    select(SerpRun.id).where(SerpRun.job_id == job.id)
                )
            )
        )
        session.execute(delete(SerpRun).where(SerpRun.job_id == job.id))
    if step_index <= 3:
        session.execute(delete(JobSource).where(JobSource.job_id == job.id))
    if step_index <= 4:
        session.execute(
            delete(CompetitorAnalysisRow).where(
                CompetitorAnalysisRow.job_id == job.id
            )
        )
    if step_index <= 5:
        session.execute(
            delete(SerpSynthesisRow).where(SerpSynthesisRow.job_id == job.id)
        )
    if step_index <= 6:
        session.execute(
            delete(EvidenceNoteRow).where(EvidenceNoteRow.job_id == job.id)
        )
    if step_index <= 7:
        session.execute(
            delete(ContentBriefRow).where(ContentBriefRow.job_id == job.id)
        )
    if step_index <= 8:
        session.execute(
            delete(ArticleOutlineRow).where(ArticleOutlineRow.job_id == job.id)
        )

    # H11: article_versions and article_reviews are IMMUTABLE append-only
    # history (spec sections 27-28, 46.13-46.14) — a retry appends new
    # versions rather than deleting the old ones, so we never delete them
    # here. The current draft/final are derived from the history (see
    # step_done), and stale older versions/reviews stay queryable for audit.
    if step_index <= 14:
        session.execute(delete(ImageRow).where(ImageRow.job_id == job.id))

    # M12 / spec section 54: LLM usage rows are an append-only cost ledger —
    # a re-run appends new rows and NEVER deletes the earlier run's. (The
    # old behaviour here deleted every re-run step's rows, which erased the
    # cost history section 54 asks us to keep for later analysis.) So there
    # is deliberately no LLMUsageRow deletion in this reset.

    return len(STEP_NAMES) - step_index + 1
