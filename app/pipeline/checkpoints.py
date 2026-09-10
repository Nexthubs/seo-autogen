"""Per-step pipeline checkpoints (P9-A — spec sections 9, 44, 51).

The 15 pipeline steps each commit a stable checkpoint row on success
(spec section 9: "save DB + structured result + status + commit + next").
This module turns those rows into the queryable map P9 needs:

- :func:`step_done` — did this step already commit its output?
- :func:`first_incomplete_step` — the resume-from-checkpoint boundary
  ("Resume from checkpoint": skip every done step, re-run from the first
  step that is not done).
- :func:`reset_from_step` — delete ONLY the outputs of the given step and
  every later step ("Retry by step": a step re-run must start from a clean
  state for its own outputs, because several steps *append* instead of
  replacing — ``serp_search`` adds a new run, ``competitor_analysis`` and
  the article writer/reviser append rows; see the per-step idempotency
  notes in the docstrings).

Deletions are done explicitly (not left to ORM/FK cascades) so they behave
identically on PostgreSQL (``ondelete=CASCADE``) and SQLite (no foreign
key pragma in unit tests).
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models.article import ArticleReviewRow, ArticleVersionRow
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
      done when the review row exists on the *writer* version that the
      reviews target (a revision version, if present, makes the reviews
      stale anyway).
    - ``article_reviser`` is done when a ``revision``-stage version exists.
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
        return _version_exists(session, job, "revision")
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

    Deletion is scope-correct per the steps' idempotency model:

    - ``serp_search`` (2) adds a run whose results cascade — delete both
      explicitly.
    - ``source_extract`` (3) owns the ``job_sources`` links.
    - ``article_writer`` (9) owns ALL versions (a fresh draft invalidates
      the revision and every review).
    - the review steps (10-12) and ``article_reviser`` (13) only invalidate
      the ``revision``-stage version (and its reviews): re-running a review
      or the revision keeps the writer draft and the other two reviews.
    - ``image_plan`` (14) owns the plan rows; ``image_generate`` (15) is
      partial-idempotent (per-image ``local_path`` overwrite) so a 15-reset
      deletes nothing.
    - ``llm_usage`` (P9-B1) rows are owned by the step that made the call:
      a re-run re-makes its calls, so their usage rows are deleted with
      the rest of the step outputs (steps 4 and 7-14 carry LLM calls).
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

    # Version rows: a writer reset (<=9) invalidates everything; a review /
    # reviser reset (10..13) only the revision stage. Reviews of the
    # deleted versions are removed explicitly (cross-dialect, see module
    # docstring).
    if step_index <= 13:
        if step_index <= 9:
            version_ids = (
                select(ArticleVersionRow.id).where(
                    ArticleVersionRow.job_id == job.id
                )
            )
        else:
            version_ids = (
                select(ArticleVersionRow.id).where(
                    ArticleVersionRow.job_id == job.id,
                    ArticleVersionRow.stage == "revision",
                )
            )
        session.execute(
            delete(ArticleReviewRow).where(
                ArticleReviewRow.article_version_id.in_(version_ids)
            )
        )
        if step_index <= 9:
            session.execute(
                delete(ArticleVersionRow).where(
                    ArticleVersionRow.job_id == job.id
                )
            )
        else:
            session.execute(
                delete(ArticleVersionRow).where(
                    ArticleVersionRow.job_id == job.id,
                    ArticleVersionRow.stage == "revision",
                )
            )
    if step_index <= 14:
        session.execute(delete(ImageRow).where(ImageRow.job_id == job.id))

    # LLM usage rows (P9-B1, spec section 54): one row per logical LLM
    # call, owned by the step that made it (steps 4-14; steps 1-3 and 15
    # make no LLM calls). A re-run re-makes those calls, so delete the
    # rows of every re-run step.
    llm_step_by_index = {
        4: "competitor_analysis",
        5: "serp_synthesis",
        6: "evidence_research",
        7: "content_brief",
        8: "outline",
        9: "article_writer",
        10: "seo_review",
        11: "fact_review",
        12: "style_review",
        13: "article_reviser",
        14: "image_plan",
    }
    steps_to_delete = [
        name
        for idx, name in llm_step_by_index.items()
        if idx >= step_index
    ]
    if steps_to_delete:
        session.execute(
            delete(LLMUsageRow).where(
                LLMUsageRow.job_id == job.id,
                LLMUsageRow.step.in_(steps_to_delete),
            )
        )

    return len(STEP_NAMES) - step_index + 1
