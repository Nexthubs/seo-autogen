"""Local image storage (SEO-AUTO-DEV-SPEC.md section 34).

Layout under ``DATA_DIR``:

    data/articles/{job_uuid}/images/
        hero.webp
        inline-1.webp
        inline-2.webp

The hero image never enters the body markdown by default
(section 3.2); it is the future ``mainImage`` for Strapi.

M07: a ``.webp`` filename MUST contain real WebP bytes. Providers may
return PNG/JPEG payloads regardless of the requested filename, so every
byte blob is transcoded (via Pillow, when available) before it touches
the local store, and every stored file is re-validated on read (magic +
decode + MIME/extension consistency).
"""

import io
from pathlib import Path

from app.core.config import get_settings
from app.core.exceptions import ErrorCode, PipelineError

try:  # Pillow is a declared dependency; degrade gracefully if absent.
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

#: Magic prefixes used by the header sniff (before the Pillow decode).
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8"
_WEBP_MAGIC = b"RIFF"


def sniff_mime(data: bytes) -> str | None:
    """Return the image MIME for raw bytes, or None when unrecognized."""
    if data.startswith(_PNG_MAGIC):
        return "image/png"
    if data[:2] == _JPEG_MAGIC:
        return "image/jpeg"
    if data[:4] == _WEBP_MAGIC and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_image_bytes(data: bytes, expected_suffix: str | None = None) -> str:
    """Validate raw image bytes; return their real MIME type.

    M07: header sniff AND (when Pillow is present) a full decode check.
    When ``expected_suffix`` is given, the sniffed format must match the
    extension (``.webp`` requires real WebP bytes — a PNG named ``.webp``
    is rejected, not silently served as WebP).
    """
    mime = sniff_mime(data)
    if mime is None:
        raise PipelineError(
            ErrorCode.IMAGE_PROVIDER_FAILED,
            "image bytes are not a recognizable image (bad header)",
        )
    if Image is not None:
        try:
            with Image.open(io.BytesIO(data)) as img:
                img.verify()
        except Exception as exc:
            raise PipelineError(
                ErrorCode.IMAGE_PROVIDER_FAILED,
                f"image bytes fail to decode: {exc}",
            ) from exc
    if expected_suffix:
        want = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(expected_suffix.lower())
        if want is not None and want != mime:
            raise PipelineError(
                ErrorCode.IMAGE_PROVIDER_FAILED,
                f"file named '{expected_suffix}' contains {mime} bytes — "
                "extension and content must match",
            )
    return mime


def ensure_webp_bytes(data: bytes) -> tuple[bytes, str]:
    """Return ``(bytes, mime)`` that are REAL WebP (M07).

    WebP input passes through; PNG/JPEG input is transcoded via Pillow.
    Without Pillow the bytes are validated but NOT transcoded — the
    caller is expected to keep the honest extension/MIME (the pipeline
    enforces a WebP extension, so a non-WebP payload there is a hard
    error).
    """
    mime = sniff_mime(data)
    if mime is None:
        raise PipelineError(
            ErrorCode.IMAGE_PROVIDER_FAILED,
            "image bytes are not a recognizable image (bad header)",
        )
    if mime == "image/webp":
        return data, mime
    if Image is None:
        raise PipelineError(
            ErrorCode.IMAGE_PROVIDER_FAILED,
            f"provider returned {mime} bytes for a .webp file and Pillow "
            "is not available to transcode",
        )
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        rgba = img.convert("RGBA")
    buf = io.BytesIO()
    rgba.save(buf, "WEBP", quality=85, method=4)
    webp = buf.getvalue()
    validate_image_bytes(webp, ".webp")  # round-trip guarantee
    return webp, "image/webp"


def validate_local_image(path: object) -> str:
    """Read a stored image file and validate it (M06/M07).

    Returns the REAL MIME. Raises ``IMAGE_PROVIDER_FAILED`` when the file
    is missing, unreadable, corrupt, or its bytes disagree with its
    extension (a ``.webp`` that is really a PNG is a hard failure — the
    step re-run must regenerate it, not serve it).
    """
    p = Path(path)
    if not p.is_file():
        raise PipelineError(
            ErrorCode.IMAGE_PROVIDER_FAILED,
            f"image file missing: {p}",
        )
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise PipelineError(
            ErrorCode.IMAGE_PROVIDER_FAILED,
            f"image file unreadable: {p}",
        ) from exc
    return validate_image_bytes(data, p.suffix or None)


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
) -> tuple[Path, str]:
    """Persist one generated image; return ``(path, real_mime)``.

    M07: a ``.webp`` filename is normalized to real WebP bytes (transcoded
    when the provider returned PNG/JPEG); any filename's bytes are
    validated against its extension before the file is written.
    """
    settings = settings or get_settings()
    root = (
        Path(storage_root)
        if storage_root is not None
        else job_images_dir(job_id, settings)
    )
    suffix = Path(filename).suffix
    if suffix.lower() == ".webp":
        data, mime = ensure_webp_bytes(data)
    else:
        mime = validate_image_bytes(data, suffix or None)
    root.mkdir(parents=True, exist_ok=True)
    path = root / filename
    path.write_bytes(data)
    return path, mime
