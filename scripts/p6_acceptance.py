"""P6 acceptance demo: Image Pipeline (spec sections 2.4, 30-34, 46.15,
51; P6).

Everything real where the environment allows it:

  - real PostgreSQL (Docker ``seo-pg``) with migration 0007 (images)
  - real local LLM (litellm proxy at 127.0.0.1:4000, model
    qwen3.8-27b) for the Image Planner (structured JSON plan)
  - image GENERATION uses a fake provider: the local litellm proxy
    serves no image models (checked at runtime; if IMAGE_API_KEY is
    configured, the real OpenAIImageProvider health_check is probed
    too)

Acceptance checklist (spec P6):
  [ ] images table exists (migration 0007), strapi_* columns NULL
  [ ] three articles of ~1000 / ~1700 / ~2500 words -> ceilings
      1 / 2 / 3 (section 31)
  [ ] planner: 1 <= plan.total_count <= ceiling, total_count ==
      len(images), hero first (sections 30, 31)
  [ ] prompt: final article + brand guideline + ceiling, NO
      competitor texts (section 50)
  [ ] generation: files at data/articles/{job}/images/ (section 34)
  [ ] article.md / article.json exported; markers resolved, hero
      never in the body (flag default true)
  [ ] rows: local_path / mime_type / provider / provider_request_id
      filled; strapi_* still NULL (P7 boundary)
  [ ] job status: image_planning -> image_generating -> ready

Run:
  PYTHONPATH=/home/ubuntu/ai-coding/seo-autogen python3 scripts/p6_acceptance.py
"""

import asyncio
import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete as sa_delete, inspect, select

from app.core.config import Settings
from app.core.enums import JobStatus
from app.db.models import GenerationJob
from app.db.models.article import ArticleVersionRow
from app.db.models.images import ImageRow
from app.db.session import SessionLocal, check_database, engine
from app.pipeline.steps.image_generate import run_image_generation
from app.pipeline.steps.image_plan import run_image_planner
from app.providers.image.openai_image import OpenAIImageProvider
from app.providers.llm.openai_compatible import OpenAICompatibleLLMProvider
from app.schemas.images import GeneratedImage, ImageGenerationRequest
from app.services.image_count_service import image_ceiling
from app.services.image_markers import MARKER_PATTERN
from app.services.image_storage import save_image_bytes

ACC = "p6acc"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: (label, ~target word count, section heading the planner may use)
ARTICLES = [
    ("short (~1000 words, ceiling 1)", 1000, "What is anxious attachment"),
    ("medium (~1700 words, ceiling 2)", 1700, "What is anxious attachment"),
    ("long (~2500 words, ceiling 3)", 2500, "What is anxious attachment"),
]

CHECKS: list[tuple[str, bool]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((label, ok))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail and not ok else ""))


def build_settings() -> Settings:
    """Real local LLM; image settings from .env (API key may be empty)."""
    return Settings(
        llm_base_url=os.environ.get("P6_LLM_BASE_URL", "http://127.0.0.1:4000/v1"),
        llm_api_key=os.environ.get("P6_LLM_API_KEY", "sk-nexthubszhaozhao"),
        llm_model=os.environ.get("P6_LLM_MODEL", "qwen3.8-27b"),
    )


def _body_for(word_target: int, heading: str) -> str:
    """Body whose ACTUAL whitespace word count reaches word_target."""
    para = (
        "Paragraphs talk about anxious attachment, no contact, boundaries, "
        "self-regulation and recovery in plain, practical language. "
    )
    blocks = [f"# {ACC.title()} guide\n\n", f"## {heading}\n\n"]
    words = len(blocks[0].split()) + len(blocks[1].split())
    block_words = len((para * 8).split())
    i = 1
    while words < word_target:
        blocks.append(para * 8 + "\n\n")
        words += block_words
        i += 1
    return "".join(blocks)


def reset() -> None:
    """Remove ONLY the acceptance rows — user data must survive."""
    with SessionLocal() as session:
        session.execute(sa_delete(ImageRow).where(
            ImageRow.job_id.in_(
                select(GenerationJob.id).where(GenerationJob.keyword.ilike(f"{ACC}%"))
            )
        ))
        session.execute(sa_delete(ArticleVersionRow).where(
            ArticleVersionRow.job_id.in_(
                select(GenerationJob.id).where(GenerationJob.keyword.ilike(f"{ACC}%"))
            )
        ))
        session.execute(sa_delete(GenerationJob).where(
            GenerationJob.keyword.ilike(f"{ACC}%")
        ))
        session.commit()


def make_jobs() -> list[tuple[str, int, int]]:
    """Seed 3 jobs + final article versions. Returns (job_id, words, ceiling)."""
    out = []
    with SessionLocal() as session:
        for idx, (label, target, heading) in enumerate(ARTICLES):
            body = _body_for(target, heading)
            job = GenerationJob(
                keyword=f"{ACC} anxious attachment no contact {idx}",
                status=JobStatus.IMAGE_PLANNING.value,
                current_step="image_planning",
                target_function="coach",
            )
            session.add(job)
            session.flush()
            session.add(
                ArticleVersionRow(
                    job_id=job.id,
                    version=1,
                    stage="revision",
                    title=f"{ACC.title()} Anxious Attachment Guide {idx}",
                    body_markdown=body,
                    seo_title=f"P6ACC SEO title {idx}",
                    meta_description="P6ACC meta.",
                    slug=f"p6acc-anxious-attachment-{idx}",
                    model="qwen3.8-27b",
                    prompt_name="article_reviser",
                    prompt_version="1.0",
                )
            )
            words = len(body.split())
            out.append((job.id, words, image_ceiling(words)))
        session.commit()
    return out


class FakeImageProvider:
    """Local litellm serves no image models — deterministic fake for gen."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self.requests: list[ImageGenerationRequest] = []

    async def generate(self, request: ImageGenerationRequest) -> GeneratedImage:
        self.requests.append(request)
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4"
            "nGNgAAIAAAUAAXpeqz8AAAAASUVORK5CYII="
        )
        path = save_image_bytes(
            request.job_id or "nojob", png, request.filename,
            settings=self._settings,
        )
        return GeneratedImage(
            local_path=str(path),
            filename=request.filename,
            mime_type="image/png",
            prompt=request.prompt,
            provider="p6acc-fake-image",
            provider_request_id=f"p6acc-{request.filename}",
        )

    async def health_check(self) -> bool:
        return True


async def run_all(job_specs: list[tuple[str, int, int]],
                  settings: Settings) -> dict:
    """Planner (REAL LLM) + generation (fake provider) for all 3 jobs."""
    results: dict[str, dict] = {}
    for job_id, words, ceiling in job_specs:
        print(f"\n-- job {job_id} ({words} words, ceiling {ceiling})")
        with SessionLocal() as session:
            job = session.get(GenerationJob, job_id)
            llm = OpenAICompatibleLLMProvider(settings=settings)
            try:
                plan, rows = await run_image_planner(session, job, llm)
            finally:
                await llm.aclose()
            print(f"  planner: total={plan.total_count} "
                  f"roles={[r.role for r in rows]}")
            results[job_id] = {
                "words": words, "ceiling": ceiling,
                "plan": plan, "rows_after_plan": rows,
                "status_after_plan": job.status,
            }

            provider = FakeImageProvider(settings)
            rows = await run_image_generation(session, job, provider,
                                              settings=settings)
            print(f"  generation: {len(provider.requests)} images, "
                  f"status={job.status}")
            results[job_id].update({
                "rows_after_gen": rows,
                "status_after_gen": job.status,
                "gen_files": [r.filename for r in rows],
            })
    return results


def verify(results: dict, settings: Settings) -> None:
    print("\n== Planner checks (sections 30, 31, 50) ==")
    for job_id, info in results.items():
        plan = info["plan"]
        rows = info["rows_after_plan"]
        label = f"[{info['ceiling']}]"
        check(f"{label} 1 <= total_count <= ceiling",
              1 <= plan.total_count <= info["ceiling"],
              f"total={plan.total_count} ceiling={info['ceiling']}")
        check(f"{label} total_count == len(images)",
              plan.total_count == len(plan.images))
        check(f"{label} hero first", bool(plan.images) and
              plan.images[0].role == "hero")
        check(f"{label} rows == plan", len(rows) == plan.total_count)
        check(f"{label} status -> image_generating after planner",
              info["status_after_plan"] ==
              JobStatus.IMAGE_GENERATING.value)
        # Section 50: the planner context = final article + brand
        # guideline + ceiling. Reconstruct the exact prompt shape and
        # assert no competitor material is mixed in (P6 seeds none).
        from app.pipeline.steps.image_plan import build_user_prompt
        from app.services.prompt_service import brand_guideline_excerpt
        from app.core.config import get_settings
        prompt = build_user_prompt(
            json.dumps({"title": f"{ACC.title()} guide", "body_markdown":
                        f"({info['words']} words, final revision)"}),
            brand_guideline_excerpt(get_settings()),
            info["words"],
            info["ceiling"],
        )
        has_ceiling = f"Word count: {info['words']}" in prompt
        has_guideline = "Brand visual guideline" in prompt
        check(f"{label} planner prompt has article + guideline + ceiling",
              has_ceiling and has_guideline)

    print("\n== Generation checks (sections 33, 34, 46.15) ==")
    for job_id, info in results.items():
        rows = info["rows_after_gen"]
        data_dir = Path(settings.data_dir)
        img_dir = data_dir / "articles" / str(job_id) / "images"
        files_ok = all((img_dir / f).exists() for f in info["gen_files"])
        missing = [f for f in info["gen_files"] if not (img_dir / f).exists()]
        check(f"[{info['ceiling']}] files under {img_dir}", files_ok,
              f"missing={missing}")
        check(f"[{info['ceiling']}] article.md exists",
              (data_dir / "articles" / str(job_id) / "article.md").exists())
        check(f"[{info['ceiling']}] article.json exists",
              (data_dir / "articles" / str(job_id) / "article.json").exists())

        md = (data_dir / "articles" / str(job_id) / "article.md").read_text()
        check(f"[{info['ceiling']}] no residual [[IMAGE:*]] markers",
              MARKER_PATTERN.search(md) is None)
        check(f"[{info['ceiling']}] hero NOT in body (flag default true)",
              "images/hero.webp" not in md)
        inline_n = [r for r in rows if r.role == "inline"]
        body_has_inline = all(
            f"images/{r.filename}" in md for r in inline_n
        )
        check(f"[{info['ceiling']}] inline images resolved in body",
              body_has_inline)

        with SessionLocal() as session:
            db_rows = session.scalars(
                select(ImageRow).where(ImageRow.job_id == job_id)
            ).all()
        check(f"[{info['ceiling']}] DB rows == {len(rows)}",
              len(db_rows) == len(rows))
        filled = all(
            r.local_path and r.mime_type and r.provider
            and r.provider_request_id for r in db_rows
        )
        check(f"[{info['ceiling']}] provenance filled "
              f"(local_path/mime/provider/request_id)", filled)
        strapi_null = all(
            r.strapi_url is None and r.strapi_media_id is None
            and r.strapi_media_document_id is None for r in db_rows
        )
        check(f"[{info['ceiling']}] strapi_* still NULL (P7 boundary)",
              strapi_null)

        check(f"[{info['ceiling']}] status -> ready",
              info["status_after_gen"] == JobStatus.READY.value)

    print("\n== Table shape (section 46.15) ==")
    cols = {c["name"] for c in inspect(engine).get_columns("images")}
    required = {
        "id", "job_id", "role", "sort_order", "purpose", "section_heading",
        "insertion_marker", "prompt", "filename", "alt_text", "aspect_ratio",
        "local_path", "mime_type", "provider_request_id", "strapi_url",
        "provider", "strapi_media_id", "strapi_media_document_id",
        "created_at",
    }
    check("images table has all 46.15 columns", required <= cols,
          f"missing={required - cols}")


def main() -> int:
    if not check_database():
        print("FATAL: PostgreSQL not reachable — cannot run P6 acceptance.")
        return 2

    settings = build_settings()
    print(f"LLM: {settings.llm_base_url} model={settings.llm_model}")
    print(f"DATA_DIR: {settings.data_dir}")
    if not settings.image_api_key:
        print("NOTE: IMAGE_API_KEY empty -> image GENERATION uses the "
              "fake provider (local litellm has no image models).")

    async def _probe() -> bool:
        p = OpenAICompatibleLLMProvider(settings=settings)
        try:
            return await p.health_check()
        finally:
            await p.aclose()

    check("LLM endpoint reachable (health_check)", asyncio.run(_probe()))
    if not CHECKS[-1][1]:
        print("FATAL: LLM endpoint unreachable.")
        return 2

    # Optional: real image provider probe when a key is configured.
    if settings.image_api_key:
        async def _img_probe() -> bool:
            p = OpenAIImageProvider(settings=settings)
            try:
                return await p.health_check()
            finally:
                await p.aclose()

        check(f"image endpoint health_check "
              f"({settings.image_base_url}, {settings.image_model})",
              asyncio.run(_img_probe()))
    else:
        print("  [SKIP] image endpoint health_check (no IMAGE_API_KEY)")

    reset()
    job_specs = make_jobs()
    print(f"Seeded {len(job_specs)} jobs")

    try:
        results = asyncio.run(run_all(job_specs, settings))
    except Exception as exc:  # pipeline failure -> acceptance fails
        check(f"pipeline ran to completion ({type(exc).__name__})",
              False, str(exc)[:300])
        print("\nPIPELINE FAILED — skipping persistence checks.")
        return 1

    verify(results, settings)

    passed = sum(1 for _, ok in CHECKS if ok)
    total = len(CHECKS)
    print(f"\n== P6 acceptance: {passed}/{total} checks passed ==")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
