"""P6 unit: image planning + generation steps (spec sections 30-34, 51).

Scripted fake LLM (planner) + fake image provider + SQLite in-memory —
no network, no external services.
"""

import json
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import JobStatus
from app.core.exceptions import ErrorCode, PipelineError
from app.db.base import Base
from app.db.models import GenerationJob
from app.db.models.article import ArticleVersionRow
from app.db.models.images import ImageRow
from app.pipeline.steps.image_generate import run_image_generation
from app.pipeline.steps.image_plan import (
    build_user_prompt,
    normalize_plan,
    run_image_planner,
)
from app.providers.llm.openai_compatible import OpenAICompatibleLLMProvider
from app.schemas.images import (
    GeneratedImage,
    ImagePlan,
    ImagePlanItem,
    ImagePlanOutput,
    ImageGenerationRequest,
)
from app.services.image_markers import insert_image_markers, resolve_image_markers
from app.services.image_storage import save_image_bytes

FAST_BACKOFF = (0.001, 0.001, 0.001)
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: Body long enough for ceiling 3 (> 2200 words).
BODY_2201 = "## What is anxious attachment\n\n" + "word " * 2300
BODY_SHORT = "## Intro\n\nA short body under a thousand words total."

PLAN_HERO_ONLY = {
    "total_count": 1,
    "images": [
        {
            "role": "hero",
            "purpose": "Whole-article emotional anchor.",
            "section_heading": None,
            "insertion_marker": None,
            "filename": "hero.webp",
            "alt_text": "A person by a quiet window at dusk",
            "prompt": "A calm figure by a window, warm light, no text.",
            "aspect_ratio": "16:9",
        }
    ],
}

PLAN_THREE = {
    "total_count": 3,
    "images": [
        {
            "role": "hero",
            "purpose": "Anchor.",
            "section_heading": None,
            "insertion_marker": None,
            "filename": "hero.webp",
            "alt_text": "Hero alt",
            "prompt": "hero prompt, no text no logos.",
            "aspect_ratio": "16:9",
        },
        {
            "role": "inline",
            "purpose": "Visualize the concept.",
            "section_heading": "What is anxious attachment",
            "insertion_marker": "inline-1",
            "filename": "inline-1.webp",
            "alt_text": "Inline one alt",
            "prompt": "inline prompt one.",
            "aspect_ratio": "4:3",
        },
        {
            "role": "inline",
            "purpose": "Close the arc.",
            "section_heading": "What is anxious attachment",
            "insertion_marker": "inline-2",
            "filename": "inline-2.webp",
            "alt_text": "Inline two alt",
            "prompt": "inline prompt two.",
            "aspect_ratio": "4:3",
        },
    ],
}


def _settings(tmp_path) -> Settings:
    return Settings(
        llm_base_url="http://llm.test/v1",
        llm_api_key="test-key",
        llm_model="test-model",
        llm_max_retries=2,
        image_model="gpt-image-2",
        data_dir=str(tmp_path),
        strapi_frontend_renders_main_image=True,
        _env_file=None,
    )


class FakeLLM:
    def __init__(self, payloads, settings):
        self.payloads = list(payloads)
        self.calls = []
        self._settings = settings
        self._provider = OpenAICompatibleLLMProvider(
            settings=settings,
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(self._handle),
                base_url="http://llm.test/v1",
            ),
            backoff_seconds=FAST_BACKOFF,
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(
            {
                "system": body["messages"][0]["content"],
                "user": body["messages"][1]["content"],
                "temperature": body.get("temperature"),
            }
        )
        return httpx.Response(
            200,
            json={
                "id": "cmpl-1",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": self.payloads.pop(0),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    async def generate_structured(self, **kwargs):
        return await self._provider.generate_structured(**kwargs)

    async def aclose(self):
        await self._provider.aclose()


PNG_1x1_BYTES = (
    b"\x89PNG\r\n\x1a\n" + b"fake-png-body-for-tests" * 3
)


class FakeImageProvider:
    """Writes a tiny PNG to the section-34 layout for every request."""

    def __init__(self, settings, *, fail_on: str | None = None):
        self._settings = settings
        self.requests: list[ImageGenerationRequest] = []
        self.fail_on = fail_on  # filename that raises

    async def generate(self, request: ImageGenerationRequest) -> GeneratedImage:
        self.requests.append(request)
        if self.fail_on and request.filename == self.fail_on:
            raise PipelineError(
                ErrorCode.IMAGE_PROVIDER_FAILED,
                f"boom generating {request.filename}",
            )
        path = save_image_bytes(
            request.job_id or "nojob",
            PNG_1x1_BYTES,
            request.filename,
            settings=self._settings,
        )
        return GeneratedImage(
            local_path=str(path),
            filename=request.filename,
            mime_type="image/png",
            prompt=request.prompt,
            provider="fake-image-model",
            provider_request_id=f"fake-{request.filename}",
        )

    async def health_check(self) -> bool:
        return True


@pytest.fixture()
def db(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            GenerationJob(
                keyword="p6test anxious attachment no contact",
                status=JobStatus.IMAGE_PLANNING.value,
                current_step="image_planning",
                target_function="coach",
            )
        )
        session.flush()
        yield session
    engine.dispose()


def _add_version(session: Session, job: GenerationJob, body: str) -> ArticleVersionRow:
    row = ArticleVersionRow(
        job_id=job.id,
        version=1,
        stage="revision",
        title="Anxious Attachment No Contact: The P6 Test Guide",
        body_markdown=body,
        seo_title="P6 SEO title",
        meta_description="P6 meta.",
        slug="p6test-anxious-attachment",
        model="test-model",
        prompt_name="article_reviser",
        prompt_version="1.0",
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------
async def test_planner_hero_only_short_article(db, tmp_path):
    job = db.scalars(select(GenerationJob)).one()
    _add_version(db, job, BODY_SHORT)
    llm = FakeLLM([json.dumps(PLAN_HERO_ONLY)], _settings(tmp_path))
    plan, rows = await run_image_planner(db, job, llm)
    await llm.aclose()

    assert plan.total_count == 1
    assert rows[0].role == "hero"
    assert rows[0].insertion_marker is None
    assert job.status == JobStatus.IMAGE_GENERATING.value

    # Prompt content (section 50): article + guideline + ceiling, no
    # competitor texts.
    user_prompt = llm.calls[0]["user"]
    assert "ceiling" in user_prompt and "HARD maximum" in user_prompt
    assert "Brand visual guideline" in user_prompt
    # Section 50: the final article is in the context.
    assert "A short body under a thousand words total." in user_prompt


async def test_planner_clamps_to_ceiling(db, tmp_path):
    """Section 31: the planner may reduce, NEVER exceed the ceiling.

    A short article (ceiling 1) + an LLM plan of 3 images must be
    clamped to the hero only.
    """
    job = db.scalars(select(GenerationJob)).one()
    _add_version(db, job, BODY_SHORT)
    llm = FakeLLM([json.dumps(PLAN_THREE)], _settings(tmp_path))
    plan, rows = await run_image_planner(db, job, llm)
    await llm.aclose()

    assert plan.total_count == 1
    assert [r.role for r in rows] == ["hero"]
    assert rows[0].filename == "hero.webp"


async def test_planner_reassigns_markers_and_filenames(db, tmp_path):
    """LLM markers are renumbered deterministically (section 33/34)."""
    job = db.scalars(select(GenerationJob)).one()
    _add_version(db, job, BODY_2201)
    sloppy = json.loads(json.dumps(PLAN_THREE))
    # Deliberately wrong marker/filename — program normalizes them.
    sloppy["images"][1]["insertion_marker"] = "img-7"
    sloppy["images"][1]["filename"] = "whatever.png"
    llm = FakeLLM([json.dumps(sloppy)], _settings(tmp_path))
    plan, rows = await run_image_planner(db, job, llm)
    await llm.aclose()

    assert [r.insertion_marker for r in rows] == [None, "inline-1", "inline-2"]
    assert [r.filename for r in rows] == [
        "hero.webp",
        "inline-1.webp",
        "inline-2.webp",
    ]
    assert [r.sort_order for r in rows] == [0, 1, 2]


async def test_planner_replaces_plan_on_rerun(db, tmp_path):
    job = db.scalars(select(GenerationJob)).one()
    _add_version(db, job, BODY_2201)
    llm = FakeLLM(
        [json.dumps(PLAN_THREE), json.dumps(PLAN_HERO_ONLY)], _settings(tmp_path)
    )
    await run_image_planner(db, job, llm)
    plan, rows = await run_image_planner(db, job, llm)
    await llm.aclose()

    assert plan.total_count == 1
    remaining = db.scalars(
        select(ImageRow).where(ImageRow.job_id == job.id)
    ).all()
    assert len(remaining) == 1
    assert remaining[0].role == "hero"


async def test_planner_requires_article(db, tmp_path):
    job = db.scalars(select(GenerationJob)).one()
    llm = FakeLLM([json.dumps(PLAN_HERO_ONLY)], _settings(tmp_path))
    with pytest.raises(PipelineError) as excinfo:
        await run_image_planner(db, job, llm)
    await llm.aclose()
    assert excinfo.value.error_code == ErrorCode.IMAGE_PLAN_INVALID


async def test_m08_image_planning_temperature_from_settings(db, tmp_path, monkeypatch):
    """M08: the planner's temperature must come from Settings
    (LLM_TEMPERATURE_IMAGE_PLANNING), not a hardcoded 0.3."""
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "llm_temperature_image_planning", 0.44)

    job = db.scalars(select(GenerationJob)).one()
    _add_version(db, job, BODY_2201)
    llm = FakeLLM([json.dumps(PLAN_HERO_ONLY)], _settings(tmp_path))
    await run_image_planner(db, job, llm)
    await llm.aclose()
    assert llm.calls[0]["temperature"] == 0.44


async def test_normalize_plan_rejects_empty():
    with pytest.raises(PipelineError) as excinfo:
        normalize_plan(ImagePlanOutput(total_count=0, images=[]), 3)
    assert excinfo.value.error_code == ErrorCode.IMAGE_PLAN_INVALID


def test_build_user_prompt_shape():
    prompt = build_user_prompt(
        '{"title": "T"}', "# guideline", 1500, 2
    )
    assert '{"title": "T"}' in prompt
    assert "# guideline" in prompt
    assert "Word count: 1500" in prompt
    assert "2" in prompt


# ---------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------
async def test_generation_hero_only_short_article(db, tmp_path):
    """Section 3.1: 1 image = mainImage only, body has NO images."""
    job = db.scalars(select(GenerationJob)).one()
    _add_version(db, job, BODY_SHORT)
    settings = _settings(tmp_path)
    llm = FakeLLM([json.dumps(PLAN_HERO_ONLY)], settings)
    await run_image_planner(db, job, llm)
    await llm.aclose()

    provider = FakeImageProvider(settings)
    rows = await run_image_generation(db, job, provider, settings=settings)

    assert [r.filename for r in rows] == ["hero.webp"]
    assert (tmp_path / "articles" / str(job.id) / "images/hero.webp").exists()
    assert rows[0].local_path.endswith("hero.webp")
    assert rows[0].provider == "fake-image-model"
    assert rows[0].provider_request_id == "fake-hero.webp"
    assert job.status == JobStatus.READY.value

    article_md = (tmp_path / "articles" / str(job.id) / "article.md").read_text()
    # Hero NOT in the body (default flag true) and no residual markers.
    assert "![" not in article_md
    assert "[[IMAGE:" not in article_md


async def test_generation_markers_resolved_in_body(db, tmp_path):
    job = db.scalars(select(GenerationJob)).one()
    body = (
        "## What is anxious attachment\n\n"
        + "A real paragraph that explains the concept here. " * 350
    )
    _add_version(db, job, body)
    settings = _settings(tmp_path)
    llm = FakeLLM([json.dumps(PLAN_THREE)], settings)
    await run_image_planner(db, job, llm)
    await llm.aclose()

    provider = FakeImageProvider(settings)
    rows = await run_image_generation(db, job, provider, settings=settings)

    assert [r.filename for r in rows] == [
        "hero.webp",
        "inline-1.webp",
        "inline-2.webp",
    ]
    img_dir = tmp_path / "articles" / str(job.id) / "images"
    assert (img_dir / "hero.webp").exists()
    assert (img_dir / "inline-1.webp").exists()
    assert (img_dir / "inline-2.webp").exists()

    article_md = (img_dir.parent / "article.md").read_text()
    assert "![Inline one alt](images/inline-1.webp)" in article_md
    assert "![Inline two alt](images/inline-2.webp)" in article_md
    assert "[[IMAGE:" not in article_md
    # Hero stays out of the body (default flag true).
    assert "images/hero.webp" not in article_md
    assert job.status == JobStatus.READY.value


async def test_h07_export_resolves_internal_link_markers(db, tmp_path):
    """H07: the local article.md export ships NO raw markers.

    The Writer emits ``[[INTERNAL_LINK:X]]`` markers (section 21); the
    final renderer must resolve them to markdown links BEFORE the export
    (image markers are inserted on top, after link resolution).
    """
    from app.schemas.internal_link import InternalLinkRule as _Rule
    from app.services.internal_link_service import upsert_rule

    job = db.scalars(select(GenerationJob)).one()
    upsert_rule(
        db,
        _Rule(
            marker="ATTACHMENT_TEST",
            anchor_text="attachment test",
            target_url="https://example.com/attachment-test",
        ),
    )
    db.commit()
    body = (
        "## What is anxious attachment\n\n"
        + "Take the "
        + "[[INTERNAL_LINK:ATTACHMENT_TEST]] "
        + "before you reach out. " * 350
    )
    _add_version(db, job, body)
    settings = _settings(tmp_path)
    llm = FakeLLM([json.dumps(PLAN_THREE)], settings)
    await run_image_planner(db, job, llm)
    await llm.aclose()
    await run_image_generation(db, job, FakeImageProvider(settings), settings=settings)

    article_md = (tmp_path / "articles" / str(job.id) / "article.md").read_text()
    # internal link resolved to a real markdown link
    assert "[attachment test](https://example.com/attachment-test)" in article_md
    # NO residual marker of either kind
    assert "[[INTERNAL_LINK:" not in article_md
    assert "[[IMAGE:" not in article_md
    # images still resolved
    assert "![Inline one alt](images/inline-1.webp)" in article_md
    assert job.status == JobStatus.READY.value


async def test_h07_export_unknown_internal_link_marker_fails(db, tmp_path):
    """H07: an unresolvable internal link marker is a pipeline error.

    No raw marker may silently ship to article.md; the DoD gate is
    supposed to catch these, so a marker that survives to the export
    step is a contract violation and must fail loudly.
    """
    from app.schemas.internal_link import InternalLinkRule as _Rule
    from app.services.internal_link_service import upsert_rule

    job = db.scalars(select(GenerationJob)).one()
    upsert_rule(
        db,
        _Rule(
            marker="ATTACHMENT_TEST",
            anchor_text="attachment test",
            target_url="https://example.com/attachment-test",
        ),
    )
    body = (
        "## What is anxious attachment\n\n"
        + "A "
        + "[[INTERNAL_LINK:GHOST_MARKER]] "
        + "that does not exist in the rules. " * 350
    )
    _add_version(db, job, body)
    settings = _settings(tmp_path)
    llm = FakeLLM([json.dumps(PLAN_THREE)], settings)
    await run_image_planner(db, job, llm)
    await llm.aclose()

    with pytest.raises(PipelineError) as excinfo:
        await run_image_generation(db, job, FakeImageProvider(settings), settings=settings)
    assert excinfo.value.error_code == ErrorCode.ARTICLE_VALIDATION_FAILED
    # the job STAYS at image_generating — the export was NOT written
    assert job.status == JobStatus.IMAGE_GENERATING.value
    assert not (tmp_path / "articles" / str(job.id) / "article.md").exists()


async def test_generation_failure_keeps_image_generating(db, tmp_path):
    """Section 9: failure stays at image_generating; re-run from here."""
    job = db.scalars(select(GenerationJob)).one()
    body = (
        "## What is anxious attachment\n\n"
        + "A real paragraph that explains the concept here. " * 350
    )
    _add_version(db, job, body)
    settings = _settings(tmp_path)
    llm = FakeLLM([json.dumps(PLAN_THREE)], settings)
    await run_image_planner(db, job, llm)
    await llm.aclose()

    provider = FakeImageProvider(settings, fail_on="inline-1.webp")
    with pytest.raises(PipelineError) as excinfo:
        await run_image_generation(db, job, provider, settings=settings)
    assert excinfo.value.error_code == ErrorCode.IMAGE_PROVIDER_FAILED
    assert job.status == JobStatus.IMAGE_GENERATING.value
    # The hero (already generated) is checkpointed on its row.
    rows_now = db.scalars(
        select(ImageRow).where(ImageRow.job_id == job.id)
    ).all()
    hero = next(r for r in rows_now if r.role == "hero")
    assert hero.local_path is not None

    # Re-run with a healthy provider succeeds from the image step.
    provider2 = FakeImageProvider(settings)
    rows = await run_image_generation(db, job, provider2, settings=settings)
    assert job.status == JobStatus.READY.value
    assert len(provider2.requests) == 3


async def test_generation_requires_plan(db, tmp_path):
    job = db.scalars(select(GenerationJob)).one()
    _add_version(db, job, BODY_SHORT)
    settings = _settings(tmp_path)
    provider = FakeImageProvider(settings)
    with pytest.raises(PipelineError) as excinfo:
        await run_image_generation(db, job, provider, settings=settings)
    assert excinfo.value.error_code == ErrorCode.IMAGE_PLAN_INVALID


async def test_hero_included_in_body_when_flag_false(db, tmp_path):
    """Section 3.2: flag false -> renderer prepends the hero."""
    job = db.scalars(select(GenerationJob)).one()
    _add_version(db, job, BODY_SHORT)
    settings = _settings(tmp_path).model_copy(
        update={"strapi_frontend_renders_main_image": False}
    )
    llm = FakeLLM([json.dumps(PLAN_HERO_ONLY)], settings)
    await run_image_planner(db, job, llm)
    await llm.aclose()

    provider = FakeImageProvider(settings)
    await run_image_generation(db, job, provider, settings=settings)

    article_md = (tmp_path / "articles" / str(job.id) / "article.md").read_text()
    assert "![A person by a quiet window at dusk](images/hero.webp)" in article_md
    # Hero image appears exactly once (prepended, no marker duplication).
    assert article_md.count("images/hero.webp") == 1


# ---------------------------------------------------------------------
# Marker service (section 33)
# ---------------------------------------------------------------------
def test_marker_insertion_after_named_section():
    body = (
        "Intro para.\n\n"
        "## Section A\n\n"
        "Para A text.\n\n"
        "More A.\n\n"
        "## Section B\n\n"
        "Para B text.\n"
    )
    out = insert_image_markers(
        body, [("inline-1", "Section A"), ("inline-2", "Section B")]
    )
    # inline-1 after the first paragraph of Section A
    assert "Para A text.\n\n[[IMAGE:inline-1]]" in out
    # inline-2 after the first paragraph of Section B
    assert "Para B text.\n\n[[IMAGE:inline-2]]" in out


def test_marker_fallback_when_heading_missing():
    body = "p one\n\np two\n\np three\n"
    out = insert_image_markers(body, [("inline-1", "No Such Heading")])
    assert "[[IMAGE:inline-1]]" in out


def test_resolve_markers_and_drop_unknown():
    body = "text\n\n[[IMAGE:inline-1]]\n\n[[IMAGE:ghost]]\n\nmore\n"
    out = resolve_image_markers(
        body, {"inline-1": ("alt1", "images/inline-1.webp")}
    )
    assert "![alt1](images/inline-1.webp)" in out
    assert "ghost" not in out
    assert "[[IMAGE:" not in out
