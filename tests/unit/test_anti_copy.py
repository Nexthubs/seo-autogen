"""P5 unit: programmatic anti-copy check (SEO-AUTO-DEV-SPEC.md
section 29).

RapidFuzz-based sentence comparison; no LLM involved.
"""

from app.services.anti_copy import (
    MIN_OVERLAP_WORDS,
    MIN_SENTENCE_WORDS,
    check_anti_copy,
)

# 16 words, long enough to trip MIN_SENTENCE_WORDS and MIN_OVERLAP_WORDS.
COPIED = (
    "Anxious attachment is a pattern where people seek constant "
    "reassurance and fear abandonment in romantic relationships."
)
SOURCE = (
    "This is the opening sentence of a competitor article. "
    + COPIED
    + " And then the article continues with unrelated filler "
    "discussions about dating advice and communication skills."
)


def test_exact_copy_flagged():
    report = check_anti_copy("## S\n\n" + COPIED + "\n\n", [("https://a.example", SOURCE)])
    assert len(report["matches"]) == 1
    assert report["has_serious_overlap"] is True
    m = report["matches"][0]
    assert m["source_url"] == "https://a.example"
    assert m["word_count"] >= MIN_OVERLAP_WORDS
    assert m["similarity"] >= 0.85


def test_near_exact_copy_flagged():
    # One word swapped — still above 0.85 similarity.
    near = COPIED.replace("reassurance", "validation")
    report = check_anti_copy(near, [("https://a.example", SOURCE)])
    assert len(report["matches"]) == 1
    assert report["matches"][0]["similarity"] >= 0.85


def test_reworded_not_flagged():
    reworded = (
        "People who worry about their relationships tend to demand "
        "repeated promises of commitment and dread being left behind."
    )
    report = check_anti_copy(reworded, [("https://a.example", SOURCE)])
    assert report["matches"] == []
    assert report["has_serious_overlap"] is False


def test_short_sentence_not_flagged_even_when_copied():
    short = "No contact is hard."  # 4 words < MIN_SENTENCE_WORDS (8)
    src = "Some intro words here. " + short + " More words after that."
    report = check_anti_copy(short, [("https://a.example", src)])
    assert report["matches"] == []


def test_copy_below_overlap_words_not_flagged():
    # 10 words: over MIN_SENTENCE_WORDS (8) but under MIN_OVERLAP_WORDS (12).
    small = "one two three four five six seven eight nine ten"
    assert len(small.split()) == 10
    src = "intro words " + small + " outro words here"
    report = check_anti_copy(small, [("https://a.example", src)])
    assert report["matches"] == []


def test_empty_sources():
    report = check_anti_copy("## S\n\n" + COPIED + "\n", [])
    assert report["matches"] == []
    assert report["sources_compared"] == 0
    assert report["has_serious_overlap"] is False


def test_multiple_sources_only_matching_one_flagged():
    other = "Completely different content about unrelated marketing topics. " * 10
    report = check_anti_copy(
        "## S\n\n" + COPIED + "\n",
        [("https://a.example", SOURCE), ("https://b.example", other)],
    )
    assert len(report["matches"]) == 1
    assert report["matches"][0]["source_url"] == "https://a.example"
    assert report["sources_compared"] == 2


def test_constants():
    assert MIN_OVERLAP_WORDS == 12
    assert MIN_SENTENCE_WORDS == 8
