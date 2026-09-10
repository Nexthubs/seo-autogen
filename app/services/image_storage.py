"""Local image storage (SEO-AUTO-DEV-SPEC.md section 34).

Layout under ``DATA_DIR``:

    data/articles/{job_uuid}/images/
        hero.webp
        inline-1.webp
        inline-2.webp

The hero image never enters the body markdown by default
(section 3.2); it is the future ``mainImage`` for Strapi.
"""

from pathlib import Path

from app.core.config import get_settings


def job_images_dir(job_id: object, settings=None) -> Path:
    """``{data_dir}/articles/{job_id}/images`` (section 34)."""
    settings = settings or get_settings()
    return Path(settings.data_dir) / "articles" / str(job_id) / "images"


def save_image_bytes(
    job_id: object,
    data: bytes,
    filename: str,
    *,
    settings=None,
    storage_root: Path | None = None,
) -> Path:
    """Persist one generated image; return its absolute path."""
    settings = settings or get_settings()
    root = (
        Path(storage_root)
        if storage_root is not None
        else job_images_dir(job_id, settings)
    )
    root.mkdir(parents=True, exist_ok=True)
    path = root / filename
    path.write_bytes(data)
    return path
