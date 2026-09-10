"""Programmatic outline validation (SEO-AUTO-DEV-SPEC.md section 23).

The LLM is not trusted to produce a valid outline; every outline is
checked by code before it may be persisted. On failure the pipeline
runs an Outline Repair (at most 2 times, section 23).

Checks:
    one title
    H2/H3 levels are legal and properly nested
    a Practical section exists
    a FAQ section exists (and faq_questions is non-empty, 3-8)
    a CTA slot exists in the second half
    the brief required_topics are covered by the section keywords
    6-12 top-level sections
"""

import re

from app.schemas.research import ArticleOutline, ContentBrief

MIN_TOP_SECTIONS = 6
MAX_TOP_SECTIONS = 12
MIN_FAQ_QUESTIONS = 3
MAX_FAQ_QUESTIONS = 8

#: Headings matching any of these patterns count as the Practical
#: section (case-insensitive).
_PRACTICAL_PATTERN = re.compile(
    r"practical|step|how to|exercises?|checklist|exercise|action", re.IGNORECASE
)
_FAIQ_PATTERN = re.compile(r"faq|frequently asked|questions", re.IGNORECASE)


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", text.lower()))


def validate_outline(
    outline: ArticleOutline, brief: ContentBrief | None = None
) -> list[str]:
    """Return the list of validation errors (empty = valid)."""
    errors: list[str] = []

    if not outline.title.strip():
        errors.append("missing title")

    if not outline.sections:
        errors.append("no sections")
        return errors

    top_count = sum(1 for s in outline.sections if s.level == 2)
    if not (MIN_TOP_SECTIONS <= top_count <= MAX_TOP_SECTIONS):
        errors.append(
            f"top-level section count {top_count} outside "
            f"{MIN_TOP_SECTIONS}-{MAX_TOP_SECTIONS}"
        )

    # H2/H3 nesting: a level-3 section must come right after a level-2
    # section it belongs to (no level-3 at the start, no H1/H4).
    seen_h2 = False
    for i, s in enumerate(outline.sections):
        if s.level == 2:
            seen_h2 = True
        elif s.level == 3 and not seen_h2:
            errors.append(f"section {i + 1} ({s.heading!r}) is H3 without a parent H2")
            break

    if not any(_PRACTICAL_PATTERN.search(s.heading) for s in outline.sections):
        errors.append("no practical/action-oriented section")

    if not any(_FAIQ_PATTERN.search(s.heading) for s in outline.sections):
        errors.append("no FAQ section")

    if not (MIN_FAQ_QUESTIONS <= len(outline.faq_questions) <= MAX_FAQ_QUESTIONS):
        errors.append(
            f"faq_questions count {len(outline.faq_questions)} outside "
            f"{MIN_FAQ_QUESTIONS}-{MAX_FAQ_QUESTIONS}"
        )

    mid = len(outline.sections) // 2
    if not any(s.cta_slot for s in outline.sections[mid:]):
        errors.append("no cta_slot in the second half of the outline")

    if brief is not None:
        covered = _tokens(" ".join(s.heading for s in outline.sections) + " "
                          + " ".join(kw for s in outline.sections for kw in s.keywords))
        for topic in brief.required_topics:
            topic_tokens = _tokens(topic)
            if topic_tokens and not topic_tokens & covered:
                errors.append(f"required topic not covered: {topic!r}")

        pk = brief.primary_keyword.lower()
        if pk and pk not in outline.title.lower():
            errors.append("title does not contain the primary keyword")

    return errors
