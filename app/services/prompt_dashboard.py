"""Prompt version dashboard (P9-B2 — spec sections 47, 48).

Answers two questions, both read-only:

* **Inventory** — for every versioned prompt (front-matter ``name`` /
  ``version``): its current ``prompt_hash`` (SHA256 of the file, section 48),
  content size, and the pipeline step that consumes it.
* **Usage** — which persisted LLM results were produced with which
  ``prompt_name`` / ``prompt_version`` / ``prompt_hash``. Every result table
  records the provenance at write time, so usage is a pure aggregate over
  those columns.

The two rulebooks (``seo_article_guideline.md``, ``brand_visual_guideline.md``)
are reported separately: they have no front matter and no result rows, only a
content hash.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import distinct, func, literal, select
from sqlalchemy.orm import Session

from app.db.models.article import ArticleReviewRow, ArticleVersionRow
from app.db.models.images import ImageRow
from app.db.models.research import (
    ArticleOutlineRow,
    CompetitorAnalysisRow,
    ContentBriefRow,
    SerpSynthesisRow,
)
from app.services.prompt_service import _PROMPTS_DIR, all_prompt_specs

#: (result model, 1-based pipeline step) per versioned prompt.
#: ``outline_repair`` shares the outline step (its repair rounds, section 23).
_PROMPT_RESULT_SOURCES: dict[str, tuple] = {
    "competitor_analyzer": (CompetitorAnalysisRow, 4),
    "serp_synthesis": (SerpSynthesisRow, 5),
    # evidence_research has no per-result prompt columns (section 46.10).
    "evidence_research": (None, 6),
    "content_brief": (ContentBriefRow, 7),
    "outline_generator": (ArticleOutlineRow, 8),
    # outline_repair is recorded only in the outline step's structured log.
    "outline_repair": (None, 8),
    "article_writer": (ArticleVersionRow, 9),
    "seo_reviewer": (ArticleReviewRow, 10),
    "fact_reviewer": (ArticleReviewRow, 11),
    "style_reviewer": (ArticleReviewRow, 12),
    "article_reviser": (ArticleVersionRow, 13),
    "image_planner": (ImageRow, 14),
}

#: ``article_reviews`` rows are one review per (version, type); the three
#: reviewers are disambiguated by ``review_type``.
_REVIEW_TYPES = {
    "seo_reviewer": "seo",
    "fact_reviewer": "fact",
    "style_reviewer": "style",
}


def _usage_source(model, prompt_name: str):
    """One SELECT of (name, version, hash, job_id) for one prompt."""
    # The provenance columns are nullable for backwards compatibility with
    # rows written before P9-B2.  A dashboard usage key is only meaningful
    # when all three parts of the provenance triple are present; filtering
    # incomplete rows here also keeps the aggregate sortable by strings.
    stmt = select(
        literal(prompt_name).label("prompt_name"),
        model.prompt_version,
        model.prompt_hash,
        model.job_id,
    ).where(
        model.prompt_version.is_not(None),
        model.prompt_hash.is_not(None),
    )
    if model is ArticleReviewRow:
        stmt = stmt.where(model.review_type == _REVIEW_TYPES[prompt_name])
    elif model is ArticleVersionRow or model is ImageRow:
        stmt = stmt.where(model.prompt_name == prompt_name)
    return stmt


def prompt_dashboard_payload(session: Session) -> dict:
    """Build the dashboard payload (inventory + usage + rulebooks).

    Usage is counted per (name, version, hash) triple; rows whose triple no
    longer matches the current file are reported under ``stale_versions``.
    """
    usage: dict[tuple[str, str, str], tuple[int, int]] = {}
    for prompt_name, (model, _step) in _PROMPT_RESULT_SOURCES.items():
        if model is None:
            continue
        src = _usage_source(model, prompt_name).subquery()
        agg = (
            select(
                src.c.prompt_name,
                src.c.prompt_version,
                src.c.prompt_hash,
                func.count().label("usage"),
                func.count(distinct(src.c.job_id)).label("jobs"),
            )
            .group_by(
                src.c.prompt_name, src.c.prompt_version, src.c.prompt_hash
            )
        )
        for row in session.execute(agg).all():
            usage[(row.prompt_name, row.prompt_version, row.prompt_hash)] = (
                row.usage,
                row.jobs,
            )

    specs = all_prompt_specs()
    prompts = []
    current: set[tuple[str, str, str]] = set()
    for spec in specs:
        key = (spec.name, spec.version, spec.prompt_hash)
        current.add(key)
        n_usage, n_jobs = usage.get(key, (0, 0))
        prompts.append(
            {
                "name": spec.name,
                "version": spec.version,
                "prompt_hash": spec.prompt_hash,
                "content_chars": len(spec.content),
                "step": _PROMPT_RESULT_SOURCES[spec.name][1],
                "usage": n_usage,
                "jobs": n_jobs,
            }
        )

    stale_versions = [
        {
            "prompt_name": key[0],
            "prompt_version": key[1],
            "prompt_hash": key[2],
            "usage": value[0],
            "jobs": value[1],
        }
        for key, value in sorted(usage.items())
        if key not in current
    ]

    prompt_stems = {p["name"] for p in prompts}
    rulebooks = [
        {
            "name": path.name,
            "hash": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(_PROMPTS_DIR.glob("*.md"))
        if path.stem not in prompt_stems
    ]

    return {
        "prompts": prompts,
        "stale_versions": stale_versions,
        "rulebooks": rulebooks,
    }
