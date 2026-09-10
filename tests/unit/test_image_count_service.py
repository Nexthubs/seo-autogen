"""P6 unit: Image Count Service (SEO-AUTO-DEV-SPEC.md section 31).

The ceiling is decided PROGRAMMATICALLY:

    < 1200 words  -> 1 (hero only)
    1200-2200     -> 2 (hero + 1 inline)
    > 2200        -> 3 (hero + 2 inline)
"""

from app.services.image_count_service import (
    IMAGE_MAX_COUNT,
    count_words,
    image_ceiling,
    suggested_image_count,
)


def test_suggested_image_count_thresholds():
    # Section 31 / spec section 2.4 table.
    assert suggested_image_count(0) == 1
    assert suggested_image_count(1) == 1
    assert suggested_image_count(1199) == 1
    assert suggested_image_count(1200) == 2
    assert suggested_image_count(1700) == 2
    assert suggested_image_count(2200) == 2
    assert suggested_image_count(2201) == 3
    assert suggested_image_count(99999) == 3


def test_image_max_count_is_three():
    # Spec section 2.4: V1_TOTAL_IMAGE_MAX = 3 (includes hero).
    assert IMAGE_MAX_COUNT == 3
    assert suggested_image_count(10**9) == IMAGE_MAX_COUNT


def test_count_words_splits_on_whitespace():
    assert count_words("") == 0
    assert count_words("one two   three") == 3
    assert count_words("a\n\nb\tc") == 3


def test_image_ceiling_never_exceeds_max():
    assert image_ceiling(1199) == 1
    assert image_ceiling(1200) == 2
    assert image_ceiling(2201) == 3
    # Even for huge articles the ceiling stays at IMAGE_MAX_COUNT.
    assert image_ceiling(10**9) == IMAGE_MAX_COUNT
