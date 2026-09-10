"""Unified Definition-of-Done gate (SEO-AUTO-DEV-SPEC.md section 63).

An article reaches ``READY`` only when EVERY section-63 bullet holds:
SERP, Research, Article and Images all pass. This module is the single
implementation of that gate; it is invoked once by the orchestrator,
unconditionally, after every successful step run (fresh run, retry,
resume, and the no-rework READY backfill) so a job can never be marked
READY with a missing or invalid artifact. Step-level callers (e.g.
``image_generate`` setting READY directly) are not gated: the gate is a
pipeline-level property and only the orchestrator owns it.

On failure the caller raises ``PipelineError(ARTICLE_VALIDATION_FAILED)``
and the job lands in ``FAILED`` with that error code. Checkpoints are
intentionally left untouched, so the failure is recoverable by retrying
the failed step (spec: terminal statuses are ready/failed/cancelled).

The check is pure read-only: it never mutates rows or commits.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.core.config import get_settings
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
from app.db.models.source import JobSource, SourcePage
from app.pipeline.steps._article_common import latest_article_version
from app.schemas.research import ArticleOutline, ContentBrief
from app.schemas.internal_link import MARKER_SYNTAX
from app.services.image_markers import _HEADING, _normalize_heading
from app.services.internal_link_service import validate_markers

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

#: ATX H1 line in the body (the writer contract forbids any H1 —
#: identical to ``article_writer._H1_LINE``, kept local to avoid an
#: import cycle through the provider stack).
_H1_LINE = re.compile(r"(?m)^#\s(?!\s)(.*)$")
#: Setext H1: a non-blank text line followed by a ``=`` underline. A
#: ``-`` underline is a Setext *H2*, not an H1 — matching only ``=``
#: keeps horizontal rules (``---``) and H2 underlines out of it.
_SETEXT_H1 = re.compile(
    r"(?m)^[^\n`#*\s][^\n]*[^\n`\s]\n[ \t]*={2,}[ \t]*\r?$"
)
#: "## FAQ" heading in the final body.
_FAQ_HEADING = re.compile(r"(?m)^##\s+(?!#)FAQ\b")
#: Any internal-link marker attempt: the double-bracket + prefix is
#: case-INSENSITIVE, so a malformed attempt (lowercase marker id
#: ``[[INTERNAL_LINK:foo]]`` or a lowercase prefix ``[[internal_link:FOO]]``)
#: is caught here. ``validate_markers``'s case-sensitive ``MARKER_SYNTAX``
#: would skip both and let a broken marker ship.
_INTERNAL_LINK_ANY = re.compile(r"\[\[INTERNAL_LINK:", re.IGNORECASE)

MIN_SOURCES = 1
MAX_SOURCES = 5
MIN_IMAGES = 1
MAX_IMAGES = 3
#: Content guideline: the FAQ section must hold at least three real,
#: high-frequency Q&A pairs (each a ``###`` question with an answer).
MIN_FAQ_QUESTIONS = 3
REQUIRED_REVIEWS = ("seo", "fact", "style")


def validate_article_done(session: "Session", job: GenerationJob) -> list[str]:
    """Return the list of unmet section-63 conditions (empty = done)."""
    errors: list[str] = []
    errors.extend(_check_serp(session, job))
    errors.extend(_check_research(session, job))
    article_errors, version = _check_article(session, job)
    errors.extend(article_errors)
    errors.extend(_check_images(session, job, version))
    return errors


# ----------------------------------------------------------------------
# 63.1 SERP
# ----------------------------------------------------------------------
def _check_serp(session: "Session", job: GenerationJob) -> list[str]:
    errors: list[str] = []
    runs = session.scalars(
        select(SerpRun).where(SerpRun.job_id == job.id)
    ).all()
    has_payload = any(run.raw_response for run in runs)
    if not has_payload:
        errors.append("serp: no SERP run with a stored raw response")

    organic = session.scalars(
        select(SerpResult)
        .join(SerpRun, SerpResult.serp_run_id == SerpRun.id)
        .where(SerpRun.job_id == job.id, SerpResult.result_type == "organic")
    ).all()
    if not organic:
        errors.append("serp: no organic SERP results")

    sources = session.scalars(
        select(SourcePage)
        .join(JobSource, JobSource.source_page_id == SourcePage.id)
        .where(JobSource.job_id == job.id)
    ).all()
    if not MIN_SOURCES <= len(sources) <= MAX_SOURCES:
        errors.append(
            f"serp: expected {MIN_SOURCES}-{MAX_SOURCES} source pages, "
            f"got {len(sources)}"
        )
    else:
        urls = [s.normalized_url for s in sources]
        if len(set(urls)) != len(urls):
            errors.append("serp: duplicate normalized_url among source pages")
    return errors


# ----------------------------------------------------------------------
# 63.2 Research
# ----------------------------------------------------------------------
def _check_research(session: "Session", job: GenerationJob) -> list[str]:
    """Research bullet (63): re-validate the stored payloads, not just the
    row presence / persisted ``valid`` flag. A stored brief or outline must
    round-trip through its Pydantic model, and every job source page must
    have a competitor analysis (the step persists exactly one per source)."""
    errors: list[str] = []
    count = session.scalars(
        select(CompetitorAnalysisRow).where(
            CompetitorAnalysisRow.job_id == job.id
        )
    ).all()
    if not count:
        errors.append("research: no competitor analysis")
    else:
        sources = session.scalars(
            select(SourcePage)
            .join(JobSource, JobSource.source_page_id == SourcePage.id)
            .where(JobSource.job_id == job.id)
        ).all()
        analysed = {row.source_page_id for row in count}
        missing = [str(page.id) for page in sources if page.id not in analysed]
        if missing:
            errors.append(
                "research: "
                f"{len(missing)} source page(s) have no competitor analysis"
            )

    if not session.scalars(
        select(SerpSynthesisRow).where(SerpSynthesisRow.job_id == job.id)
    ).first():
        errors.append("research: no SERP synthesis")

    evidence = session.scalars(
        select(EvidenceNoteRow).where(EvidenceNoteRow.job_id == job.id)
    ).all()
    if not evidence:
        errors.append("research: no evidence notes")

    brief_row = session.scalars(
        select(ContentBriefRow).where(ContentBriefRow.job_id == job.id)
    ).first()
    if brief_row is None:
        errors.append("research: no content brief")
    else:
        try:
            ContentBrief.model_validate(brief_row.brief or {})
        except Exception:
            errors.append(
                "research: stored content brief fails ContentBrief validation"
            )

    outlines = session.scalars(
        select(ArticleOutlineRow).where(ArticleOutlineRow.job_id == job.id)
    ).all()
    if not outlines:
        errors.append("research: no article outline")
    else:
        latest = outlines[-1]
        # ``valid`` is persisted by the outline step (it re-repairs on a
        # failed validation). Re-validate the payload itself too: a stored
        # outline that does not round-trip through ArticleOutline is invalid
        # regardless of the flag.
        if not latest.valid:
            errors.append("research: latest article outline failed validation")
        else:
            try:
                ArticleOutline.model_validate(latest.outline or {})
            except Exception:
                errors.append("research: latest article outline failed validation")
    return errors


# ----------------------------------------------------------------------
# 63.3 Article
# ----------------------------------------------------------------------
def _check_article(
    session: "Session", job: GenerationJob
) -> tuple[list[str], ArticleVersionRow | None]:
    errors: list[str] = []
    version = latest_article_version(session, job)
    if version is None:
        return ["article: no article version persisted"], None

    # H11: the shipped (latest) version must be the reviser's output, not a
    # bare writer draft. A writer retry appends a new draft after a stale
    # revision; if the reviser never re-ran, the latest version would be a
    # writer draft and the job is NOT done — it must not be silently shipped.
    if version.stage != "revision":
        errors.append(
            "article: latest version v"
            f"{version.version} is stage '{version.stage}', expected "
            "'revision' — the reviser has not produced the final version"
        )

    body = version.body_markdown or ""
    if not (version.title or "").strip():
        errors.append("article: title is empty")
    if _H1_LINE.search(body):
        errors.append("article: body_markdown contains an H1 line")
    if _SETEXT_H1.search(body):
        errors.append(
            "article: body_markdown contains a Setext H1 (= underline)"
        )
    if not (version.seo_title or "").strip():
        errors.append("article: seo_title is empty")
    if not (version.meta_description or "").strip():
        errors.append("article: meta_description is empty")
    if not (version.slug or "").strip():
        errors.append("article: slug is empty")
    if not _FAQ_HEADING.search(body):
        errors.append("article: no FAQ section in body_markdown")
    else:
        faq_questions = _faq_question_count(body)
        if faq_questions < MIN_FAQ_QUESTIONS:
            errors.append(
                "article: FAQ section has "
                f"{faq_questions} question(s), at least {MIN_FAQ_QUESTIONS} "
                "Q&A pairs are required (content guideline)"
            )

    errors.extend(_check_cta(session, job, version))

    validation = validate_markers(session, body)
    if not validation.valid:
        details = ", ".join(
            list(validation.unknown_markers) + list(validation.inactive_markers)
        )
        errors.append(f"article: invalid internal link markers ({details})")
    #: ``validate_markers`` only recognises well-formed uppercase marker
    #: ids; a malformed attempt (wrong case / bad marker body) slips past
    #: it and would ship raw. Scan the raw text case-insensitively.
    malformed = _malformed_internal_link_markers(body)
    if malformed:
        errors.append(
            "article: malformed internal link marker syntax ("
            + ", ".join(malformed)
            + ")"
        )

    #: Pipeline order: writer v(N) -> reviewers (reviews land on v(N))
    #: -> reviser v(N+1) -> FINAL anti-copy on v(N+1). So seo/fact/style
    #: must exist for SOME version of this job, while the anti-copy
    #: verdict must belong to the latest (final) version — the body that
    #: actually ships.
    job_reviews = session.scalars(
        select(ArticleReviewRow).where(ArticleReviewRow.job_id == job.id)
    ).all()
    final_reviews = session.scalars(
        select(ArticleReviewRow).where(
            ArticleReviewRow.article_version_id == version.id
        )
    ).all()
    by_type = {r.review_type: r for r in job_reviews}
    final_by_type = {r.review_type: r for r in final_reviews}
    for rtype in REQUIRED_REVIEWS:
        if rtype not in by_type:
            errors.append(f"article: missing {rtype} review")
    if "anticopy" not in final_by_type:
        errors.append(
            f"article: missing anticopy review for final version v{version.version}"
        )
    else:
        report = final_by_type["anticopy"].review or {}
        if report.get("has_serious_overlap"):
            errors.append("article: anti-copy check reports serious overlap")
    return errors, version


def _check_cta(
    session: "Session", job: GenerationJob, version: ArticleVersionRow
) -> list[str]:
    """CTA bullet (63): the outline designates a cta_slot section and that
    section exists in the final body."""
    outline_row = session.scalars(
        select(ArticleOutlineRow).where(ArticleOutlineRow.job_id == job.id)
    ).first()
    if outline_row is None:
        return []  # missing outline is already reported under research
    outline = outline_row.outline or {}
    sections = outline.get("sections")
    if not isinstance(sections, list):
        return []  # malformed outline is already reported under research
    cta_sections = [
        s.get("heading")
        for s in sections
        if isinstance(s, dict) and s.get("cta_slot") and s.get("heading")
    ]
    if not cta_sections:
        return ["article: outline has no cta_slot section"]
    body = version.body_markdown or ""
    body_headings = set()
    for line in body.splitlines():
        m = _HEADING.match(line)
        if m:
            body_headings.add(_normalize_heading(m.group(2)))
    present = [h for h in cta_sections if _normalize_heading(h) in body_headings]
    if not present:
        return ["article: none of the outline's cta_slot sections appear in the body"]
    #: A cta_slot section must not be an empty heading: the CTA content
    #: rule (content guideline) needs actual call-to-action text under it.
    for heading in present:
        if not _section_has_content(body, heading):
            return [
                f"article: cta_slot section {heading!r} has no CTA content "
                "under the heading"
            ]
    return []


def _section_has_content(body: str, heading: str) -> bool:
    """True when the section body between ``heading`` and the next
    ``##``-level heading contains a non-blank, non-heading line."""
    lines = body.splitlines()
    for idx, line in enumerate(lines):
        m = _HEADING.match(line)
        if m and _normalize_heading(m.group(2)) == _normalize_heading(heading):
            for rest in lines[idx + 1 :]:
                rest_m = _HEADING.match(rest)
                if rest_m and len(rest_m.group(1)) == 2:
                    break  # next level-2 heading: the section ends
                if rest_m:
                    continue  # a heading (e.g. ### sub) is not body text
                if rest.strip():
                    return True
            return False
    return False


def _faq_question_count(body: str) -> int:
    """Count ``###`` questions inside the FAQ section (each is one Q&A
    pair; the content guideline requires at least three real questions)."""
    lines = body.splitlines()
    for idx, line in enumerate(lines):
        m = _HEADING.match(line)
        if not (m and m.group(1) == "##" and _normalize_heading(m.group(2)) == "faq"):
            continue
        count = 0
        for rest in lines[idx + 1 :]:
            rest_m = _HEADING.match(rest)
            if rest_m and rest_m.group(1) == "##":
                break  # next level-2 heading ends the FAQ section
            if rest_m and rest_m.group(1) == "###":
                count += 1
        return count
    return 0


def _malformed_internal_link_markers(body: str) -> list[str]:
    """Case-insensitive scan for marker attempts the case-sensitive
    ``MARKER_SYNTAX`` extractor skipped (wrong case / bad marker body).
    Any attempt that is not a well-formed ``[[INTERNAL_LINK:<A-Z0-9_>]]``
    token is malformed. Returns the offending raw tokens (deduped)."""
    malformed: list[str] = []
    for m in _INTERNAL_LINK_ANY.finditer(body):
        start = m.start()
        end = body.find("]]", start)
        if end == -1:
            malformed.append(body[start : start + 32] + "…")
            continue
        token = body[start : end + 2]
        if not MARKER_SYNTAX.fullmatch(token):
            malformed.append(token)
    # Drop duplicates, keep order.
    return list(dict.fromkeys(malformed))


# ----------------------------------------------------------------------
# 63.4 Images
# ----------------------------------------------------------------------
def _check_images(
    session: "Session",
    job: GenerationJob,
    version: ArticleVersionRow | None,
) -> list[str]:
    errors: list[str] = []
    images = sorted(
        session.scalars(
            select(ImageRow).where(ImageRow.job_id == job.id)
        ).all(),
        key=lambda img: img.sort_order,
    )
    if not MIN_IMAGES <= len(images) <= MAX_IMAGES:
        errors.append(
            f"images: expected {MIN_IMAGES}-{MAX_IMAGES} images, "
            f"got {len(images)}"
        )
        return errors

    #: sort_order is 0-based (image_plan.persist_plan): the hero is #0.
    hero = next((img for img in images if img.sort_order == 0), None)
    if hero is None:
        errors.append("images: no hero image at sort_order 0")
        return errors
    if hero.role != "hero":
        errors.append(f"images: sort_order 0 image role is {hero.role!r}, not hero")
    if hero.insertion_marker is not None:
        errors.append("images: hero image must not carry an insertion marker")

    body = version.body_markdown if version is not None else ""
    settings = get_settings()
    if settings.strapi_frontend_renders_main_image and body:
        if f"[[IMAGE:{hero.filename}]]" in body:
            errors.append(
                "images: hero filename is referenced in the body although "
                "the Strapi frontend renders the main image"
            )
    if not (hero.alt_text or "").strip():
        errors.append("images: hero image has no alt_text")
    if not _image_file_exists(hero.local_path):
        errors.append(
            "images: hero image local_path is missing "
            f"(expected a generated file at {hero.local_path!r})"
        )
    if hero.filename != "hero.webp":
        errors.append(
            f"images: hero filename is {hero.filename!r}, expected 'hero.webp'"
        )

    body_headings = set()
    if body:
        for line in body.splitlines():
            m = _HEADING.match(line)
            if m:
                body_headings.add(_normalize_heading(m.group(2)))

    #: The writer body has NO [[IMAGE:*]] markers — they are inserted by
    #: ``insert_image_markers`` at render time. "Inline 图插入合理位置"
    #: therefore means: the planned section heading exists in the body
    #: (a missing heading degrades the insertion to the even-spread
    #: fallback, which is exactly the bad placement this guards against).
    inline_no = 0
    for img in images:
        if img.sort_order == 0:
            continue
        inline_no += 1
        expected = f"inline-{inline_no}"
        if img.role != "inline":
            errors.append(
                f"images: sort_order {img.sort_order} image role is "
                f"{img.role!r}, not inline"
            )
        if img.insertion_marker != expected:
            errors.append(
                f"images: inline image sort_order {img.sort_order} has "
                f"marker {img.insertion_marker!r}, expected {expected!r}"
            )
        if img.filename != f"{expected}.webp":
            errors.append(
                f"images: inline image sort_order {img.sort_order} has "
                f"filename {img.filename!r}, expected '{expected}.webp'"
            )
        if not (img.alt_text or "").strip():
            errors.append(
                f"images: inline image sort_order {img.sort_order} has "
                "no alt_text"
            )
        if not _image_file_exists(img.local_path):
            errors.append(
                f"images: inline image sort_order {img.sort_order} local_path "
                f"is missing (expected a generated file at {img.local_path!r})"
            )
        if body and img.section_heading and _normalize_heading(
            img.section_heading
        ) not in body_headings:
            errors.append(
                f"images: inline image section heading {img.section_heading!r} "
                "is not in the article body"
            )
    return errors


def _image_file_exists(local_path: str | None) -> bool:
    """A generated image row must point at a real file on disk.

    ``local_path`` is set by ``image_generate`` for every generated image;
    a missing (or dangling) path means the step did not actually produce
    the file, so the article would ship a reference to nothing.
    """
    if not local_path:
        return False
    return os.path.isfile(local_path)
