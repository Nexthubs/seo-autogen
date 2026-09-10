"""P4 unit: programmatic outline validation (spec section 23)."""

import copy

import pytest

from app.schemas.research import ArticleOutline, ContentBrief, OutlineSection
from app.services.outline_validator import (
    MAX_FAQ_QUESTIONS,
    MAX_TOP_SECTIONS,
    MIN_FAQ_QUESTIONS,
    MIN_TOP_SECTIONS,
    validate_outline,
)


def _brief(**overrides) -> ContentBrief:
    base = dict(
        primary_keyword="avoidant attachment",
        search_intent="informational",
        search_stage="awareness",
        article_strategy="guide",
        target_reader="reader",
        core_problem="confusion",
        emotional_context="frustration",
        unique_angle="honest guide",
        required_topics=["no contact", "signs"],
        recommended_word_count=2500,
    )
    base.update(overrides)
    return ContentBrief(**base)


def _valid_outline() -> ArticleOutline:
    """A minimal outline that passes every rule."""
    return ArticleOutline(
        title="Avoidant Attachment: a Complete Practical Guide",
        sections=[
            OutlineSection(heading="What is avoidant attachment", level=2, purpose="define", keywords=["avoidant attachment"]),
            OutlineSection(heading="No contact explained", level=2, purpose="explain", keywords=["no contact"]),
            OutlineSection(heading="Signs of an avoidant partner", level=2, purpose="list", keywords=["signs"]),
            OutlineSection(heading="Practical steps", level=2, purpose="action", keywords=["steps"]),
            OutlineSection(heading="Common exercises", level=2, purpose="practice", keywords=["exercises"], cta_slot=True),
            OutlineSection(heading="FAQ", level=2, purpose="answer", keywords=["faq"]),
        ],
        faq_questions=["q1?", "q2?", "q3?"],
    )


def test_valid_outline_passes():
    assert validate_outline(_valid_outline(), _brief()) == []


def test_missing_title():
    o = _valid_outline()
    o.title = "   "
    errors = validate_outline(o)
    assert any("title" in e for e in errors)


def test_title_must_contain_primary_keyword():
    o = _valid_outline()
    o.title = "A Random Guide"
    errors = validate_outline(o, _brief())
    assert any("primary keyword" in e for e in errors)


def test_top_level_section_count_bounds():
    o = _valid_outline()
    # too few H2s
    o.sections = o.sections[:2]
    assert any(str(MIN_TOP_SECTIONS) in e or "top-level" in e for e in validate_outline(o))
    # too many H2s
    o2 = _valid_outline()
    for i in range(8):
        o2.sections.insert(i, OutlineSection(heading=f"extra {i}", level=2, purpose="x"))
    assert any("top-level" in e for e in validate_outline(o2))


def test_h3_without_parent_h2():
    o = _valid_outline()
    o.sections.insert(0, OutlineSection(heading="orphan h3", level=3, purpose="x"))
    errors = validate_outline(o)
    assert any("without a parent H2" in e for e in errors)


def test_nested_h3_after_h2_is_legal():
    o = _valid_outline()
    o.sections.insert(
        2, OutlineSection(heading="sub note", level=3, purpose="x")
    )
    assert validate_outline(o, _brief()) == []


def test_no_practical_section():
    o = _valid_outline()
    o.sections = [
        s.model_copy(update={"heading": "Theory part"}) if "Practical" in s.heading else s
        for s in o.sections
    ]
    o.sections = [
        s.model_copy(update={"heading": "Practice drills"}) if "exercises" in s.heading.lower() else s
        for s in o.sections
    ]
    errors = validate_outline(o)
    assert any("practical" in e for e in errors)


def test_no_faq_section():
    o = _valid_outline()
    o.sections = [
        s.model_copy(update={"heading": "Final notes"}) if s.heading == "FAQ" else s
        for s in o.sections
    ]
    errors = validate_outline(o)
    assert any("FAQ" in e for e in errors)


def test_faq_question_count_bounds():
    o = _valid_outline()
    o.faq_questions = ["q1?", "q2?"]
    errors = validate_outline(o)
    assert any(str(MIN_FAQ_QUESTIONS) in e for e in errors)
    o.faq_questions = [f"q{i}?" for i in range(MAX_FAQ_QUESTIONS + 1)]
    errors = validate_outline(o)
    assert any(str(MAX_FAQ_QUESTIONS) in e for e in errors)


def test_cta_slot_must_be_in_second_half():
    o = _valid_outline()
    o.sections = [s.model_copy(update={"cta_slot": False}) for s in o.sections]
    o.sections[0] = o.sections[0].model_copy(update={"cta_slot": True})
    errors = validate_outline(o)
    assert any("cta_slot" in e for e in errors)


def test_required_topic_coverage():
    o = _valid_outline()
    updated = []
    for s in o.sections:
        if s.heading == "What is avoidant attachment":
            updated.append(s.model_copy(update={"keywords": ["avoidant attachment"]}))
        elif s.heading == "No contact explained":
            updated.append(s.model_copy(update={"keywords": ["no contact"]}))
        else:
            updated.append(s.model_copy(update={"keywords": []}))
    o.sections = updated
    # "no contact" + "avoidant attachment" survive via headings;
    # "signs" disappears only if the brief needs something uncovered.
    errors = validate_outline(o, _brief(required_topics=["no contact", "neuroscience"]))
    assert any("neuroscience" in e for e in errors)
    # and with the original brief, heading text still counts as coverage
    assert validate_outline(o, _brief()) == []


def test_required_topic_case_insensitive():
    o = _valid_outline()
    o.sections[1] = o.sections[1].model_copy(
        update={"keywords": ["NO CONTACT"]}
    )
    assert validate_outline(o, _brief()) == []


def test_no_brief_skips_brief_rules():
    o = _valid_outline()
    o.title = "no keyword here"
    o.faq_questions = ["only one"]
    errors = validate_outline(o)  # no brief
    assert not any("primary keyword" in e for e in errors)
    assert not any("required topic" in e for e in errors)


def test_empty_sections():
    o = ArticleOutline(title="t", sections=[])
    assert "no sections" in validate_outline(o)
