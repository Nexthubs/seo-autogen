"""Slug normalization + local Markdown renderer
(SEO-AUTO-DEV-SPEC.md sections 5, 6.2).

The slug is produced by the program, never taken as-is from the LLM:

    LLM suggested slug -> normalize_slug() -> uniqueness check

The local export ``article.md`` renders front matter + ONE H1::

    ---
    metaTitle: ...
    metaDescription: ...
    slug: ...
    ---

    # {{ title }}

    {{ body_markdown }}

``body_markdown`` itself never contains an H1 (spec section 5).
"""

import re
import unicodedata
from pathlib import Path

#: Slug length cap (Strapi friendly).
MAX_SLUG_CHARS = 200

_NON_SLUG = re.compile(r"[^a-z0-9]+")
_TRAILING = re.compile(r"^-+|-+$")


def normalize_slug(suggested: str, title: str | None = None) -> str:
    """Normalize a suggested slug to strict kebab-case (section 6.2).

    Lowercase -> ASCII transliteration -> kebab-case -> dedupe
    hyphens -> trim. Falls back to the title when the suggestion is
    empty/whitespace-only.
    """
    raw = (suggested or "").strip() or (title or "").strip()
    ascii_text = unicodedata.normalize("NFKD", raw)
    ascii_text = ascii_text.encode("ascii", "ignore").decode("ascii")
    slug = _NON_SLUG.sub("-", ascii_text.lower())
    slug = _TRAILING.sub("", slug)
    return slug[:MAX_SLUG_CHARS].rstrip("-")


def slug_is_unique(
    taken_slugs: set[str], slug: str
) -> bool:
    """Uniqueness check against already-existing slugs (section 6.2)."""
    return slug not in taken_slugs


def disambiguate_slug(slug: str, taken_slugs: set[str]) -> str:
    """Return ``slug`` or ``slug-2``, ``slug-3``, ... until unique."""
    candidate = slug
    n = 2
    while candidate in taken_slugs:
        candidate = f"{slug}-{n}"
        n += 1
    return candidate


def render_article_markdown(
    *,
    title: str,
    body_markdown: str,
    seo_title: str,
    meta_description: str,
    slug: str,
) -> str:
    """Render the local ``article.md`` export (section 5).

    Exactly one H1 — the title. ``body_markdown`` must be H1-free
    (enforced by the ArticleDocument validator).
    """
    front = (
        "---\n"
        f"metaTitle: {seo_title}\n"
        f"metaDescription: {meta_description}\n"
        f"slug: {slug}\n"
        "---\n\n"
        f"# {title}\n\n"
        f"{body_markdown.rstrip()}\n"
    )
    return front


def export_article(
    path: Path | str,
    *,
    title: str,
    body_markdown: str,
    seo_title: str,
    meta_description: str,
    slug: str,
) -> Path:
    """Write ``article.md`` locally and return the path (P5 acceptance)."""
    out = Path(path)
    if out.parent and str(out.parent) not in ("", "."):
        out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        render_article_markdown(
            title=title,
            body_markdown=body_markdown,
            seo_title=seo_title,
            meta_description=meta_description,
            slug=slug,
        ),
        encoding="utf-8",
    )
    return out
