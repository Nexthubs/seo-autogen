"""Step: image generation (SEO-AUTO-DEV-SPEC.md sections 3, 33, 34, 51).

Runs AFTER image planning (status ``image_generating``).

For every planned image the configured ``ImageProvider`` is called
(one request per image; retry policy is the provider's, section 51).
Each result is stored in ``data/articles/{job}/images/`` (section 34)
and written back onto the job's ``images`` row.

After all images succeed:

- the internal markers ``[[IMAGE:inline-N]]`` are replaced in the
  final body with the local image references (section 33);
- the local export ``article.md`` (section 5.1 / 34) is written with
  the hero OUT of the body (section 3.1/3.2, default
  ``STRAPI_FRONTEND_RENDERS_MAIN_IMAGE=true``);
- the job moves to ``ready`` — P7 (Strapi sync) picks it up from there.

A provider failure aborts the step with ``IMAGE_PROVIDER_FAILED`` and
the job STAYS at ``image_generating`` (section 9 checkpoint: re-run
starts at the image step, never at earlier steps).
"""

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.enums import ImageRole, JobStatus
from app.core.exceptions import ErrorCode, PipelineError
from app.db.models.images import ImageRow
from app.db.models.job import GenerationJob
from app.pipeline.steps._article_common import latest_article_version
from app.providers.image.base import ImageProvider
from app.schemas.images import ImageGenerationRequest
from app.services.article_renderer import export_article
from app.services.final_body import render_final_body

logger = logging.getLogger(__name__)


def _job_dir(job: GenerationJob, settings=None):
    from pathlib import Path

    settings = settings or get_settings()
    return Path(settings.data_dir) / "articles" / str(job.id)


def load_planned_images(
    session: Session, job: GenerationJob
) -> list[ImageRow]:
    """The job's current image plan rows, in reading order."""
    return list(
        session.scalars(
            select(ImageRow)
            .where(ImageRow.job_id == job.id)
            .order_by(ImageRow.sort_order)
        ).all()
    )


async def run_image_generation(
    session: Session,
    job: GenerationJob,
    provider: ImageProvider,
    *,
    settings=None,
) -> list[ImageRow]:
    """Generate every planned image and finalize the local article."""
    settings = settings or get_settings()

    job.status = JobStatus.IMAGE_GENERATING.value
    job.current_step = "image_generating"
    session.flush()

    version = latest_article_version(session, job)
    if version is None:
        raise PipelineError(
            ErrorCode.IMAGE_PLAN_INVALID,
            "no final article to render images into",
        )

    planned = load_planned_images(session, job)
    if not planned:
        raise PipelineError(
            ErrorCode.IMAGE_PLAN_INVALID,
            "no image plan — run the image planner first",
        )

    # Generate every planned image (section 51: per-image retries are
    # inside the provider; a hard failure aborts the step here).
    results: dict[int, tuple[str, str, str]] = {}  # sort_order -> (path, mime, req_id)
    for row in planned:
        request = ImageGenerationRequest(
            prompt=row.prompt,
            filename=row.filename,
            alt_text=row.alt_text,
            aspect_ratio=row.aspect_ratio,
            job_id=str(job.id),
        )
        image = await provider.generate(request)
        results[row.sort_order] = (
            image.local_path,
            image.mime_type,
            image.provider_request_id or "",
        )
        # Provenance on the row (section 9 checkpoint: partial results
        # survive a failure; the step re-run regenerates the missing
        # ones and overwrites the files in place).
        row.local_path = image.local_path
        row.mime_type = image.mime_type
        row.provider = image.provider
        row.provider_request_id = image.provider_request_id or None
        row.provider_cost = image.provider_cost
        session.commit()

    # Marker body (sections 21 + 33, H07): the SHARED final renderer
    # resolves internal link markers FIRST, then image markers, then
    # asserts no residual marker ships to the local export.
    placements = [
        (row.insertion_marker, row.section_heading)
        for row in planned
        if row.role == ImageRole.INLINE.value and row.insertion_marker
    ]
    # Resolve image markers with LOCAL references (P6). P7 (Strapi sync)
    # re-renders through the SAME renderer with the Strapi media URLs.
    hero = next(r for r in planned if r.role == ImageRole.HERO.value)
    images_map = {
        row.insertion_marker: (row.alt_text, f"images/{row.filename}")
        for row in planned
        if row.role == ImageRole.INLINE.value and row.insertion_marker
    }
    include_hero = None
    if not settings.strapi_frontend_renders_main_image:
        include_hero = (hero.alt_text, f"images/{hero.filename}")
    final_body = render_final_body(
        session,
        version.body_markdown,
        images=images_map,
        placements=placements,
        include_hero=include_hero,
    )

    # Local export (sections 5.1, 34): article.md + article.json.
    job_dir = _job_dir(job, settings)
    export_article(
        job_dir / "article.md",
        title=version.title,
        body_markdown=final_body,
        seo_title=version.seo_title,
        meta_description=version.meta_description,
        slug=version.slug,
    )
    (job_dir / "article.json").write_text(
        json.dumps(
            {
                "title": version.title,
                "body_markdown": final_body,
                "seo_title": version.seo_title,
                "meta_description": version.meta_description,
                "slug": version.slug,
                "images": [
                    {
                        "role": row.role,
                        "filename": row.filename,
                        "local_path": row.local_path,
                        "alt_text": row.alt_text,
                        "strapi_url": row.strapi_url,
                    }
                    for row in planned
                ],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    job.status = JobStatus.READY.value
    job.current_step = "image_generation"
    session.commit()

    logger.info(
        "image_generation_done",
        extra={
            "event": "image_generation_done",
            "job_id": str(job.id),
            "image_count": len(planned),
            "files": [row.filename for row in planned],
            "hero_in_body": not settings.strapi_frontend_renders_main_image,
        },
    )
    return planned
