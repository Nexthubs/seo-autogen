"""H-1 unit: unified Definition-of-Done gate (spec section 63).

The gate is the single point of admission to READY: it must return no
errors for a fully compliant job and must name every unmet section-63
bullet (SERP / Research / Article / Images) for a broken one.

SQLite in-memory, pure read-only gate — no network, no external services.
"""

import os
import tempfile
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, create_engine, select
from sqlalchemy.orm import Session

from app.core.enums import JobStatus
from app.db.base import Base
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
from app.services.article_dod import validate_article_done

BODY = (
    "## What is anxious attachment\n\n"
    "A person with an anxious attachment style worries that closeness "
    "will not be reciprocated, so they seek repeated reassurance.\n\n"
    "## Practical steps for dodtest\n\n"
    "Write down one trigger each evening and name the fear it activates. "
    "Then delay the reassurance request by one hour and note the result.\n\n"
    "## FAQ\n\n"
    "### What is anxious attachment?\n\n"
    "A pattern of seeking reassurance and fearing abandonment.\n\n"
    "### How long does the practice take?\n\n"
    "Most people notice a shift in a few weeks of daily practice.\n\n"
    "### Is no contact part of the practice?\n\n"
    "Not here — this article focuses on self-regulation before contact.\n"
)

#: Outline that round-trips through ``ArticleOutline``: a title, at least
#: one cta_slot section, and level/purpose on every section.
OUTLINE = {
    "title": "Anxious Attachment: A Practical Dodtest Guide",
    "sections": [
        {"heading": "What is anxious attachment", "level": 2,
         "purpose": "Define the attachment style", "keywords": ["anxious attachment"],
         "cta_slot": False},
        {"heading": "Practical steps for dodtest", "level": 2,
         "purpose": "Give a concrete practice", "keywords": ["steps"],
         "cta_slot": True},
        {"heading": "FAQ", "level": 2, "purpose": "Answer real questions",
         "keywords": ["faq"], "cta_slot": False},
    ],
    "faq_questions": [
        "What is anxious attachment?",
        "How long does the practice take?",
        "Is no contact part of the practice?",
    ],
}

#: Content brief that round-trips through ``ContentBrief``.
BRIEF = {
    "primary_keyword": "dodtest anxious attachment no contact",
    "secondary_keywords": ["no contact"],
    "long_tail_keywords": ["how long no contact"],
    "search_intent": "informational",
    "search_stage": "recovery",
    "article_strategy": "definitive guide",
    "target_reader": "adults in recovery",
    "core_problem": "reassurance seeking",
    "emotional_context": "anxious",
    "unique_angle": "evidence-based practice",
    "content_gaps": ["research findings"],
    "required_topics": ["practice", "signs"],
    "faq_questions": ["What is anxious attachment?"],
    "target_function": "coach",
    "cta_strategy": ["mid", "end"],
    "internal_link_markers": ["ATTACHMENT_TEST"],
    "recommended_word_count": 1800,
}

#: A valid one-per-source CompetitorAnalysis payload.
COMPETITOR_ANALYSIS = {
    "source_id": "00000000-0000-0000-0000-000000000000",
    "content_type": "guide",
    "search_intent": "informational",
    "estimated_word_count": 1200,
    "headings": ["What is it", "Steps"],
    "pain_points": ["reassurance seeking"],
    "key_topics": ["attachment"],
    "practical_advice": ["practice daily"],
    "faq_topics": ["how long"],
    "strengths": ["clear steps"],
    "weaknesses": ["no evidence"],
    "potential_gaps": ["research findings"],
}

#: A real, generated file must exist on disk for the DoD local_path check.
_TMP_IMG_DIR = tempfile.mkdtemp(prefix="dod-img-")

#: Minimal valid 1x1 PNG (the file only needs to exist; this is real bytes).
_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\x0dIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\x0dIDATx\x9cc\x80\x01\x00\x00\x00\xff\xff\x03\x00\x00\x06\x00\x05\x57\xbf\xab\xd4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as sess:
        yield sess
    engine.dispose()


def _source_page(sess: Session, n: int) -> SourcePage:
    page = SourcePage(
        url=f"https://dodtest.example.org/page-{n}",
        normalized_url=f"https://dodtest.example.org/page-{n}",
        url_hash=uuid.uuid4().hex,
        title=f"Page {n}",
        domain="dodtest.example.org",
        content_markdown=f"Unique body text for page {n} of the test fixture.",
        content_hash=uuid.uuid4().hex,
        extractor="fake-extractor",
        first_seen_at=datetime.now(timezone.utc),
        last_fetched_at=datetime.now(timezone.utc),
    )
    sess.add(page)
    sess.flush()
    return page


def _write_image_file(filename: str, job_id) -> str:
    """Write a real 1x1 PNG so the DoD local_path file check passes."""
    path = os.path.join(_TMP_IMG_DIR, f"{job_id}_{filename}")
    with open(path, "wb") as fh:
        fh.write(_PNG_1X1)
    return path


def _job_sources(sess: Session, job: GenerationJob) -> list[SourcePage]:
    return sess.scalars(
        select(SourcePage)
        .join(JobSource, JobSource.source_page_id == SourcePage.id)
        .where(JobSource.job_id == job.id)
    ).all()


def _build_compliant_job(sess: Session) -> GenerationJob:
    """A job with every section-63 artifact present and valid."""
    job = GenerationJob(
        keyword="dodtest anxious attachment no contact",
        status=JobStatus.IMAGE_GENERATING.value,
        current_step="image_generation",
        target_function="coach",
    )
    sess.add(job)
    sess.flush()

    # --- 63.1 SERP ------------------------------------------------------
    run = SerpRun(
        job_id=job.id,
        provider="dataforseo",
        query=job.keyword,
        location_code=2840,
        language_code="en",
        device="desktop",
        raw_response={"organic_results_count": 2},
    )
    sess.add(run)
    sess.flush()
    for rank in (1, 2):
        sess.add(
            SerpResult(
                serp_run_id=run.id,
                result_type="organic",
                rank=rank,
                url=f"https://dodtest.example.org/page-{rank}",
                raw_item={"url": f"https://dodtest.example.org/page-{rank}"},
            )
        )

    pages = [_source_page(sess, 1), _source_page(sess, 2)]
    for rank, page in enumerate(pages, start=1):
        sess.add(
            JobSource(
                job_id=job.id,
                source_page_id=page.id,
                serp_rank=rank,
                source_role="competitor",
            )
        )

    # --- 63.2 Research --------------------------------------------------
    # competitor analysis: one per source page (the DoD now requires full
    # per-source coverage, and each payload must validate as
    # CompetitorAnalysis).
    for page in pages:
        sess.add(
            CompetitorAnalysisRow(
                job_id=job.id,
                source_page_id=page.id,
                analysis={**COMPETITOR_ANALYSIS, "source_id": str(page.id)},
            )
        )
    sess.add(SerpSynthesisRow(job_id=job.id, synthesis={"user_intent": "howto"}))
    sess.add(
        EvidenceNoteRow(
            job_id=job.id,
            claim="Anxious attachment predicts reassurance-seeking.",
            source_title="Page 1",
            source_url=pages[0].url,
            source_type="competitor",
            confidence="high",
            usage="supported",
        )
    )
    sess.add(ContentBriefRow(job_id=job.id, brief=BRIEF))
    sess.add(
        ArticleOutlineRow(job_id=job.id, outline=OUTLINE, valid=True, repair_count=0)
    )

    # --- 63.3 Article ---------------------------------------------------
    writer = ArticleVersionRow(
        job_id=job.id,
        version=1,
        stage="draft",
        title="Anxious Attachment: A Practical Dodtest Guide",
        body_markdown=BODY,
        seo_title="Anxious Attachment Dodtest Guide",
        meta_description="Practical dodtest guide for anxious attachment.",
        slug="dodtest-anxious-attachment",
        model="test-model",
        prompt_name="article_writer",
        prompt_version="1.0",
    )
    sess.add(writer)
    sess.flush()
    final = ArticleVersionRow(
        job_id=job.id,
        version=2,
        stage="revision",
        title="Anxious Attachment: A Practical Dodtest Guide",
        body_markdown=BODY,
        seo_title="Anxious Attachment Dodtest Guide",
        meta_description="Practical dodtest guide for anxious attachment.",
        slug="dodtest-anxious-attachment",
        model="test-model",
        prompt_name="article_reviser",
        prompt_version="1.0",
    )
    sess.add(final)
    sess.flush()

    #: Pipeline order: the three reviews persist on the draft (v1), the
    #: FINAL anti-copy verdict on the latest (v2) version.
    for rtype in ("seo", "fact", "style"):
        sess.add(
            ArticleReviewRow(
                job_id=job.id,
                article_version_id=writer.id,
                review_type=rtype,
                review={"ok": True, "review_type": rtype},
            )
        )
    sess.add(
        ArticleReviewRow(
            job_id=job.id,
            article_version_id=final.id,
            review_type="anticopy",
            review={"matches": [], "has_serious_overlap": False},
        )
    )

    # --- 63.4 Images ----------------------------------------------------
    # The DoD checks that every generated image's local_path points at a
    # real file, so the fixture writes a real (1x1 png) file per row.
    hero_path = _write_image_file("hero.webp", job.id)
    inline_path = _write_image_file("inline-1.webp", job.id)
    sess.add(
        ImageRow(
            job_id=job.id,
            role="hero",
            sort_order=0,
            purpose="Anchor.",
            section_heading=None,
            insertion_marker=None,
            prompt="hero prompt",
            filename="hero.webp",
            local_path=hero_path,
            alt_text="A calm figure by a window",
            aspect_ratio="16:9",
            provider="mock",
        )
    )
    sess.add(
        ImageRow(
            job_id=job.id,
            role="inline",
            sort_order=1,
            purpose="Visualize the steps.",
            section_heading="Practical steps for dodtest",
            insertion_marker="inline-1",
            prompt="inline prompt",
            filename="inline-1.webp",
            local_path=inline_path,
            alt_text="A person writing in a journal",
            aspect_ratio="4:3",
            provider="mock",
        )
    )
    sess.flush()
    return job


def _job(sess: Session) -> GenerationJob:
    job = _build_compliant_job(sess)
    sess.commit()
    return job


def _final_version(sess: Session, job: GenerationJob) -> ArticleVersionRow:
    versions = sess.scalars(
        select(ArticleVersionRow).where(ArticleVersionRow.job_id == job.id)
    ).all()
    return max(versions, key=lambda v: v.version)


def _contains(errors: list[str], needle: str) -> bool:
    return any(needle in e for e in errors)


# ----------------------------------------------------------------------
# Baseline
# ----------------------------------------------------------------------
def test_compliant_job_passes_the_gate(session):
    job = _job(session)
    assert validate_article_done(session, job) == []


def test_gate_is_read_only(session):
    """The gate must not leave pending changes behind (spec: pure check)."""
    job = _job(session)
    assert not (session.new or session.dirty or session.deleted)
    assert validate_article_done(session, job) == []
    assert not (
        session.new or session.dirty or session.deleted
    ), "the gate must not add, modify, or delete rows"


# ----------------------------------------------------------------------
# 63.3 Article failure modes
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda v: v.__setattr__("title", "   "), "article: title is empty"),
        (lambda v: v.__setattr__("slug", ""), "article: slug is empty"),
        (
            lambda v: v.__setattr__("seo_title", "  "),
            "article: seo_title is empty",
        ),
        (
            lambda v: v.__setattr__("meta_description", ""),
            "article: meta_description is empty",
        ),
        (
            lambda v: v.__setattr__("body_markdown", "# Top level H1\n\n" + BODY),
            "article: body_markdown contains an H1 line",
        ),
        (
            lambda v: v.__setattr__(
                "body_markdown",
                BODY.split("## FAQ")[0] + "No FAQ section here at all.\n",
            ),
            "article: no FAQ section in body_markdown",
        ),
    ],
    ids=[
        "empty-title",
        "empty-slug",
        "missing-seo-title",
        "empty-meta",
        "h1-in-body",
        "missing-faq",
    ],
)
def test_article_field_failures(session, mutate, needle):
    job = _job(session)
    mutate(_final_version(session, job))
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(errors, needle), errors
    assert len(errors) == 1, errors


def test_missing_review_row_fails_the_gate(session):
    job = _job(session)
    style = session.scalars(
        select(ArticleReviewRow).where(
            ArticleReviewRow.job_id == job.id,
            ArticleReviewRow.review_type == "style",
        )
    ).one()
    session.delete(style)
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(errors, "article: missing style review"), errors
    assert len(errors) == 1, errors


def test_missing_anticopy_on_final_version_fails(session):
    """seo/fact/style on v1 is not enough: the anti-copy verdict must
    belong to the FINAL (latest) version — the body that ships."""
    job = _job(session)
    anticopy = session.scalars(
        select(ArticleReviewRow).where(
            ArticleReviewRow.job_id == job.id,
            ArticleReviewRow.review_type == "anticopy",
        )
    ).one()
    session.delete(anticopy)
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(errors, "missing anticopy review for final version v2"), errors
    assert len(errors) == 1, errors


def test_serious_overlap_on_final_version_fails(session):
    job = _job(session)
    anticopy = session.scalars(
        select(ArticleReviewRow).where(
            ArticleReviewRow.job_id == job.id,
            ArticleReviewRow.review_type == "anticopy",
        )
    ).one()
    anticopy.review = {"matches": [1], "has_serious_overlap": True}
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(errors, "anti-copy check reports serious overlap"), errors
    assert len(errors) == 1, errors


def test_unknown_internal_link_marker_fails(session):
    job = _job(session)
    _final_version(session, job).body_markdown = (
        BODY + "\nSee [[INTERNAL_LINK:NO_SUCH_RULE]] for more.\n"
    )
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(errors, "invalid internal link markers"), errors
    assert _contains(errors, "NO_SUCH_RULE"), errors
    assert len(errors) == 1, errors


# ----------------------------------------------------------------------
# 63.1 SERP failure modes
# ----------------------------------------------------------------------
def test_missing_serp_run_fails(session):
    job = _job(session)
    run = session.scalars(
        select(SerpRun).where(SerpRun.job_id == job.id)
    ).one()
    session.delete(run)
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(errors, "serp: no SERP run with a stored raw response"), errors
    assert _contains(errors, "serp: no organic SERP results"), errors
    # Sources survive (they are not on the run): count check still passes.
    assert not _contains(errors, "expected 1-5 source pages"), errors


def test_duplicate_source_urls_fail(session):
    job = _job(session)
    pages = _job_sources(session, job)
    pages[1].normalized_url = pages[0].normalized_url
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(errors, "duplicate normalized_url among source pages"), errors
    assert len(errors) == 1, errors


def test_too_many_source_pages_fail(session):
    job = _job(session)
    for n in (3, 4, 5, 6):
        page = _source_page(session, n)
        session.add(
            JobSource(
                job_id=job.id,
                source_page_id=page.id,
                serp_rank=n,
                source_role="competitor",
            )
        )
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(errors, "expected 1-5 source pages, got 6"), errors


# ----------------------------------------------------------------------
# 63.2 Research failure modes
# ----------------------------------------------------------------------
def test_invalid_outline_fails(session):
    job = _job(session)
    outline = session.scalars(
        select(ArticleOutlineRow).where(ArticleOutlineRow.job_id == job.id)
    ).one()
    outline.valid = False
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(errors, "latest article outline failed validation"), errors
    assert len(errors) == 1, errors


def test_missing_research_rows_fail(session):
    job = _job(session)
    session.execute(
        delete(SerpSynthesisRow).where(SerpSynthesisRow.job_id == job.id)
    )
    session.execute(
        delete(CompetitorAnalysisRow).where(
            CompetitorAnalysisRow.job_id == job.id
        )
    )
    session.execute(
        delete(EvidenceNoteRow).where(EvidenceNoteRow.job_id == job.id)
    )
    session.execute(delete(ContentBriefRow).where(ContentBriefRow.job_id == job.id))
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(errors, "research: no competitor analysis"), errors
    assert _contains(errors, "research: no SERP synthesis"), errors
    assert _contains(errors, "research: no evidence notes"), errors
    assert _contains(errors, "research: no content brief"), errors
    assert len(errors) == 4, errors


# ----------------------------------------------------------------------
# 63.4 Images failure modes
# ----------------------------------------------------------------------
def test_bad_image_plan_fails(session):
    job = _job(session)
    rows = session.scalars(
        select(ImageRow).where(ImageRow.job_id == job.id)
    ).all()
    hero = next(r for r in rows if r.sort_order == 0)
    inline = next(r for r in rows if r.sort_order == 1)
    hero.insertion_marker = "hero-1"  # hero must be marker-less
    hero.alt_text = "   "  # hero needs alt text
    inline.filename = "inline-2.webp"  # wrong numbering
    inline.section_heading = "Heading not in the body"
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(errors, "hero image must not carry an insertion marker"), errors
    assert _contains(errors, "hero image has no alt_text"), errors
    assert _contains(errors, "expected 'inline-1.webp'"), errors
    assert _contains(errors, "is not in the article body"), errors
    assert len(errors) == 4, errors


def test_missing_images_fail(session):
    job = _job(session)
    session.execute(delete(ImageRow).where(ImageRow.job_id == job.id))
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(errors, "expected 1-3 images, got 0"), errors


def test_no_article_version_fails(session):
    job = _job(session)
    session.execute(
        delete(ArticleReviewRow).where(ArticleReviewRow.job_id == job.id)
    )
    session.execute(
        delete(ArticleVersionRow).where(ArticleVersionRow.job_id == job.id)
    )
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(errors, "article: no article version persisted"), errors
    # With no body the CTA/inline-heading checks degrade to no errors.
    assert not _contains(errors, "cta_slot"), errors


# ----------------------------------------------------------------------
# 63.2 H06: stored payloads re-validated against the real schemas
# ----------------------------------------------------------------------
def test_missing_per_source_analysis_fails(session):
    """A source page with no CompetitorAnalysisRow fails the gate (H06)."""
    job = _job(session)
    page = _source_page(session, 3)
    session.add(
        JobSource(
            job_id=job.id,
            source_page_id=page.id,
            serp_rank=3,
            source_role="competitor",
        )
    )
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(errors, "source page(s) have no competitor analysis"), errors


def test_corrupt_content_brief_fails(session):
    """A stored brief that does not round-trip through ContentBrief fails."""
    job = _job(session)
    brief = session.scalars(
        select(ContentBriefRow).where(ContentBriefRow.job_id == job.id)
    ).one()
    brief.brief = {"primary_keyword": 123}  # wrong type: int, not str
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(
        errors, "stored content brief fails ContentBrief validation"
    ), errors


def test_corrupt_outline_fails(session):
    """A stored outline that does not round-trip through ArticleOutline fails
    even when the persisted ``valid`` flag is still True."""
    job = _job(session)
    outline = session.scalars(
        select(ArticleOutlineRow).where(ArticleOutlineRow.job_id == job.id)
    ).one()
    # Keep valid=True but make the payload invalid (no title, bad section).
    outline.outline = {"sections": [{"heading": "x"}]}
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(errors, "latest article outline failed validation"), errors


# ----------------------------------------------------------------------
# 63.3 H06: body structure — Setext H1, FAQ, CTA content, malformed markers
# ----------------------------------------------------------------------
def test_se_text_h1_fails(session):
    """A Setext ``=`` underline H1 in the body is rejected (H06)."""
    job = _job(session)
    version = _final_version(session, job)
    version.body_markdown = "My title\n=======\n\n## Body\n\ntext\n"
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(errors, "Setext H1"), errors


def test_h2_underline_is_not_an_h1(session):
    """A Setext ``-`` underline is an H2, not an H1 — must not be flagged."""
    job = _job(session)
    version = _final_version(session, job)
    body = version.body_markdown or ""
    body += "\nAn H2 underline\n------------------\n\nmore text\n"
    version.body_markdown = body
    session.flush()
    errors = validate_article_done(session, job)
    assert not _contains(errors, "Setext H1"), errors
    assert not _contains(errors, "contains an H1 line"), errors


def test_faq_with_too_few_questions_fails(session):
    """The FAQ section must hold at least three ``###`` questions (H06)."""
    job = _job(session)
    version = _final_version(session, job)
    version.body_markdown = (
        "## What is anxious attachment\n\ntext\n\n"
        "## Practical steps for dodtest\n\ntext\n\n"
        "## FAQ\n\n### One question only?\n\nanswer\n\n### Another?\n\nanswer\n"
    )
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(
        errors, "FAQ section has 2 question(s)"
    ), errors


def test_faq_empty_answers_do_not_lower_count(session):
    """Question count is by ### heading, but a missing FAQ heading fails."""
    job = _job(session)
    version = _final_version(session, job)
    # Drop the FAQ heading entirely.
    version.body_markdown = (
        "## What is anxious attachment\n\ntext\n\n"
        "## Practical steps for dodtest\n\ntext\n"
    )
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(errors, "no FAQ section"), errors


def test_cta_slot_section_without_content_fails(session):
    """A cta_slot section heading present but empty is rejected (H06)."""
    job = _job(session)
    version = _final_version(session, job)
    version.body_markdown = (
        "## What is anxious attachment\n\ntext\n\n"
        "## Practical steps for dodtest\n\n"  # empty CTA section
        "## FAQ\n\n### q1?\n\na\n\n### q2?\n\na\n\n### q3?\n\na\n"
    )
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(errors, "has no CTA content"), errors


def test_lowercase_internal_link_marker_fails(session):
    """A lowercase marker slips past validate_markers but is flagged (H06)."""
    job = _job(session)
    version = _final_version(session, job)
    version.body_markdown = (
        (version.body_markdown or "")
        + "\nSee [[internal_link:foo]] for more.\n"
    )
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(errors, "malformed internal link marker"), errors


def test_lowercase_marker_prefix_fails(session):
    """A lowercase prefix with a valid marker id is also malformed (H06)."""
    job = _job(session)
    version = _final_version(session, job)
    version.body_markdown = (
        (version.body_markdown or "")
        + "\nSee [[Internal_link:FOO]] for more.\n"
    )
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(errors, "malformed internal link marker"), errors


# ----------------------------------------------------------------------
# 63.4 H06: image local_path must point at a real file
# ----------------------------------------------------------------------
def test_image_local_path_missing_fails(session):
    """A dangling local_path (no file on disk) is rejected (H06)."""
    job = _job(session)
    hero = session.scalars(
        select(ImageRow).where(ImageRow.job_id == job.id, ImageRow.sort_order == 0)
    ).one()
    hero.local_path = os.path.join(_TMP_IMG_DIR, "does-not-exist.webp")
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(errors, "hero image local_path is missing"), errors


def test_image_local_path_none_fails(session):
    """A hero image with local_path=None has no file and is rejected."""
    job = _job(session)
    hero = session.scalars(
        select(ImageRow).where(ImageRow.job_id == job.id, ImageRow.sort_order == 0)
    ).one()
    hero.local_path = None
    session.flush()
    errors = validate_article_done(session, job)
    assert _contains(errors, "hero image local_path is missing"), errors
