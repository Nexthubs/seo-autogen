"""Image Count Service (SEO-AUTO-DEV-SPEC.md section 31).

The image count ceiling is decided PROGRAMMATICALLY from the final
article length; the LLM Image Planner may suggest FEWER images but
never more (section 31). The absolute cap is ``IMAGE_MAX_COUNT=3``
(spec section 2.4, ``V1_TOTAL_IMAGE_MAX``).
"""

from app.core.config import get_settings

#: The hard cap, including the hero image (spec section 2.4).
IMAGE_MAX_COUNT = 3
#: The minimum total (hero only).
IMAGE_MIN_COUNT = 1

#: Word-count thresholds (spec section 2.4 default rule / 31).
_LOW_THRESHOLD = 1200
_HIGH_THRESHOLD = 2200


def count_words(markdown: str) -> int:
    """Count words in Markdown text (headings/labels count too — the
    planner reasons about the whole document)."""
    return len((markdown or "").split())


def suggested_image_count(word_count: int) -> int:
    """Programmatic ceiling (spec section 31).

    | final article length | total images | composition        |
    |---|---:|---|
    | < 1200 words         | 1 | 1 hero             |
    | 1200-2200 words      | 2 | hero + 1 inline    |
    | > 2200 words         | 3 | hero + 2 inline    |
    """
    if word_count < _LOW_THRESHOLD:
        return 1
    if word_count <= _HIGH_THRESHOLD:
        return 2
    return 3


def image_ceiling(word_count: int) -> int:
    """The count the planner may NOT exceed (section 31 + IMAGE_MAX_COUNT)."""
    settings = get_settings()
    cap = min(settings.image_max_count, IMAGE_MAX_COUNT)
    return max(IMAGE_MIN_COUNT, min(suggested_image_count(word_count), cap))
