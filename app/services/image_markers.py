"""Inline image markers (SEO-AUTO-DEV-SPEC.md section 33).

The Article Writer never decides image placement. The Image Planner
chooses the insertion sections; this module inserts the internal
markers into the final body:

    [[IMAGE:inline-1]]
    [[IMAGE:inline-2]]

Once image generation is done, ``resolve_image_markers`` replaces each
marker with the generated image (P6: local relative path; P7 will
replace it with the Strapi media URL before sync).

The hero NEVER gets a marker (section 3.1).
"""

import logging
import re

logger = logging.getLogger(__name__)

MARKER_PATTERN = re.compile(r"\[\[IMAGE:([\w.-]+)\]\]")

_HEADING = re.compile(r"^(#{2,6})\s+(.*?)\s*#*\s*$")


def _normalize_heading(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _paragraph_ends(lines: list[str]) -> list[int]:
    """Last line index of every non-empty block paragraph."""
    ends: list[int] = []
    i = 0
    while i < len(lines):
        if lines[i].strip():
            j = i
            while j < len(lines) and lines[j].strip():
                j += 1
            ends.append(j - 1)
            i = j
        else:
            i += 1
    return ends


def _insert_after(
    lines: list[str], marker: str, after_index: int
) -> list[str]:
    """Insert ``[[IMAGE:marker]]`` as its own paragraph after the
    paragraph that ends at ``after_index`` (line index)."""
    out = list(lines[: after_index + 1])
    if out and out[-1].strip():
        out.append("")
    out.append(f"[[IMAGE:{marker}]]")
    out.append("")
    out.extend(lines[after_index + 1 :])
    return out


def insert_image_markers(
    body_markdown: str,
    placements: list[tuple[str, str | None]],
) -> str:
    """Insert one ``[[IMAGE:<marker>]]`` paragraph per placement.

    ``placements`` is a list of ``(insertion_marker, section_heading)``
    in reading order (inline items only — the hero has no marker).
    The marker lands as its own paragraph AFTER the first paragraph of
    the named section (the paragraph following the heading). When the
    heading is missing or its first paragraph is already taken, an
    even spread through the body is used instead (warning is logged).
    """
    total = len(placements)
    result = body_markdown.splitlines()
    used_headings: set[str] = set()

    for k, (marker, heading) in enumerate(placements):
        target: int | None = None
        norm = _normalize_heading(heading) if heading else ""
        if heading and norm and norm not in used_headings:
            # Heading positions in the CURRENT body (indices shift after
            # each insertion).
            for idx, line in enumerate(result):
                m = _HEADING.match(line)
                if m and _normalize_heading(m.group(2)) == norm:
                    # First paragraph after the heading line.
                    j = idx + 1
                    while j < len(result) and not result[j].strip():
                        j += 1
                    if j < len(result):
                        while j < len(result) and result[j].strip():
                            j += 1
                        end = j - 1
                        if end >= idx + 1:
                            target = end
                            break
        if target is not None:
            used_headings.add(norm)
        else:
            # Even spread fallback: paragraph k+1 of total+1 of the body.
            ends = _paragraph_ends(result)
            if ends:
                frac = (k + 1) / (total + 1)
                order = min(int(frac * len(ends)), len(ends) - 1)
                target = ends[order]
            else:
                target = len(result) - 1
            logger.warning(
                "image_marker_fallback",
                extra={
                    "event": "image_marker_fallback",
                    "marker": marker,
                    "section_heading": heading,
                },
            )
        result = _insert_after(result, marker, target)

    return "\n".join(result) + ("" if body_markdown.endswith("\n") else "")


def resolve_image_markers(
    body_markdown: str,
    images: dict[str, tuple[str, str]],
    *,
    include_hero: tuple[str, str] | None = None,
) -> str:
    """Replace ``[[IMAGE:*]]`` markers with generated images.

    ``images`` maps insertion_marker -> (alt_text, src) where ``src``
    is the (relative) image location. ``include_hero`` is
    ``(alt_text, src)`` when the hero must be prepended to the body
    (section 3.2, ``STRAPI_FRONTEND_RENDERS_MAIN_IMAGE=false``); when
    None (the default) the hero stays OUT of the body.
    """

    def _replace(match: re.Match) -> str:
        item = images.get(match.group(1))
        if item is None:
            # Unknown marker (plan changed after generation) — drop.
            return ""
        alt, src = item
        return f"![{alt}]({src})"

    out = MARKER_PATTERN.sub(_replace, body_markdown)
    if include_hero is not None:
        alt, src = include_hero
        out = f"![{alt}]({src})\n\n" + out
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out
