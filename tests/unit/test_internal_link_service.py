"""P3 unit: internal link service (spec section 21).

LLM output contains ONLY ``[[INTERNAL_LINK:MARKER]]`` markers — never URLs.
The service validates the marker against the rule set and resolves it to a
markdown link. Unknown / inactive markers must be reported, not silently
resolved or dropped.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import InternalLinkRule
from app.schemas.internal_link import (
    InternalLinkRule as Rule,
    is_valid_marker,
    extract_markers,
)
from app.services import internal_link_service as ils


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_marker_syntax_validation():
    assert is_valid_marker("ATTACHMENT_TEST")
    assert is_valid_marker("A")
    assert is_valid_marker("LINK_2")
    assert not is_valid_marker("")
    assert not is_valid_marker("lowercase")
    assert not is_valid_marker("HAS SPACE")
    assert not is_valid_marker("HAS-DASH")
    assert not is_valid_marker("x" * 65)


def test_extract_markers():
    md = "text [[INTERNAL_LINK:ATTACHMENT_TEST]] more [[INTERNAL_LINK:AVOIDANCE]]"
    assert extract_markers(md) == ["ATTACHMENT_TEST", "AVOIDANCE"]
    assert extract_markers("no markers here") == []
    # lowercase / malformed are NOT valid markers
    assert extract_markers("[[internal_link:abc]]") == []
    assert extract_markers("[[INTERNAL_LINK:bad-name]]") == []


def test_upsert_rule_roundtrip(db: Session):
    rule = ils.upsert_rule(
        db,
        Rule(
            marker="ATTACHMENT_TEST",
            anchor_text="an attachment test article",
            target_url="https://example.com/attachment-test",
            topic="attachment",
            keywords=["attachment test"],
            active=True,
        ),
    )
    db.commit()
    assert rule.id is not None

    # Re-upsert updates, does not duplicate.
    rule2 = ils.upsert_rule(
        db,
        Rule(
            marker="ATTACHMENT_TEST",
            anchor_text="changed anchor",
            target_url="https://example.com/attachment-test-v2",
            keywords=["attachment test", "secure base"],
        ),
    )
    db.commit()
    assert rule2.id == rule.id
    assert rule2.target_url == "https://example.com/attachment-test-v2"
    assert len(ils.list_rules(db)) == 1


def test_upsert_rejects_bad_marker(db: Session):
    with pytest.raises(ValueError):
        ils.upsert_rule(db, Rule(marker="bad marker", target_url="https://x.com"))


def test_resolve_markers_replaces_with_markdown_link(db: Session):
    ils.upsert_rule(
        db,
        Rule(
            marker="ATTACHMENT_TEST",
            anchor_text="attachment test",
            target_url="https://example.com/attachment-test",
        ),
    )
    ils.upsert_rule(
        db,
        Rule(marker="AVOIDANCE", target_url="https://example.com/avoidance"),
    )
    db.commit()

    md = (
        "Intro. See the [[INTERNAL_LINK:ATTACHMENT_TEST]] for details and "
        "[[INTERNAL_LINK:AVOIDANCE]] for the other side."
    )
    rendered, resolved, validation = ils.resolve_markers(db, md)

    assert (
        rendered
        == "Intro. See the [attachment test](https://example.com/attachment-test) "
        "for details and [AVOIDANCE](https://example.com/avoidance) for the other side."
    )
    assert validation.valid is True
    assert validation.unknown_markers == []
    assert [r.marker for r in resolved] == ["ATTACHMENT_TEST", "AVOIDANCE"]
    # anchor defaults to the marker itself
    assert resolved[1].anchor_text == "AVOIDANCE"


def test_unknown_marker_is_reported_not_resolved(db: Session):
    ils.upsert_rule(
        db, Rule(marker="KNOWN", target_url="https://example.com/k")
    )
    db.commit()
    md = "a [[INTERNAL_LINK:GHOST]] b [[INTERNAL_LINK:KNOWN]] c"
    rendered, resolved, validation = ils.resolve_markers(db, md)

    assert validation.valid is False
    assert validation.unknown_markers == ["GHOST"]
    assert [r.marker for r in resolved] == ["KNOWN"]
    # unknown marker text is left in place, not silently dropped
    assert "[[INTERNAL_LINK:GHOST]]" in rendered
    assert "[known](https://example.com/k)" in rendered.lower() or (
        "](https://example.com/k)" in rendered
    )


def test_inactive_marker_is_reported_not_resolved(db: Session):
    ils.upsert_rule(
        db,
        Rule(marker="PAUSED", target_url="https://example.com/p", active=False),
    )
    db.commit()
    md = "[[INTERNAL_LINK:PAUSED]]"
    rendered, resolved, validation = ils.resolve_markers(db, md)
    assert validation.valid is False
    assert validation.inactive_markers == ["PAUSED"]
    assert resolved == []
    assert "[[INTERNAL_LINK:PAUSED]]" in rendered


def test_validate_markers_only(db: Session):
    ils.upsert_rule(db, Rule(marker="A1", target_url="https://example.com/a1"))
    db.commit()
    validation = ils.validate_markers(db, "x [[INTERNAL_LINK:A1]] y [[INTERNAL_LINK:B2]]")
    assert validation.valid is False
    assert validation.used_markers == ["A1", "B2"]
    assert validation.unknown_markers == ["B2"]
