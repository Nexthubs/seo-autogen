"""Audit R-H06: evidence verification must check *support*, not reachability.

The first H09 fix proved a source URL answered with non-empty text. The
audit reproduced the gap: a fabricated paper/number paired with a live
"Welcome to our homepage. Contact us for details." page still shipped as
``high / supported``.

``assess_source_support`` (pure function) and ``verify_evidence_sources``
now compare the fetched body against the note:

* source title corroboration (author / distinctive token / year),
* every claimed number present in one of its spellings,
* enough claim-term overlap with the body,
* no contradiction markers in a claim-related sentence.

The required negative cases are: reachable-but-irrelevant, contradicted,
missing number, and mismatched paper title.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.exceptions import PipelineError
from app.providers.extractor.base import ContentExtractor
from app.schemas.research import EvidenceNote
from app.schemas.sources import ExtractedPage
from app.services.evidence_verification import (
    assess_source_support,
    verify_evidence_sources,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _note(**over) -> EvidenceNote:
    base = dict(
        claim="Attachment styles affect adult relationships.",
        source_title="Hazan & Shaver 1987",
        source_url="https://doi.org/10.1037/0022-3514.53.3.519",
        source_type="peer-reviewed study",
        confidence="high",
        usage="supported",
        note="cite properly",
    )
    base.update(over)
    return EvidenceNote(**base)


def _page(content: str, title: str | None = "V") -> ExtractedPage:
    return ExtractedPage(
        url="https://doi.org/10.1037/0022-3514.53.3.519",
        normalized_url="https://doi.org/10.1037/0022-3514.53.3.519",
        title=title,
        content_markdown=content,
        extracted_at=NOW,
        extractor="fake",
    )


class TestAssessSourceSupport:
    def test_supported_when_title_terms_and_number_corroborate(self):
        check = assess_source_support(
            _note(claim="97% of adults show an attachment style."),
            _page(
                "Hazan and Shaver (1987) reported that 97% of adults show "
                "an attachment style in romantic relationships."
            ),
        )
        assert check.status == "supported"
        assert check.excerpt is not None
        assert "97%" in check.excerpt

    def test_reachable_but_irrelevant_is_unsupported(self):
        check = assess_source_support(
            _note(),
            _page("Welcome to our homepage. Contact us for details."),
        )
        assert check.status == "unsupported"
        assert "source title" in check.reason or "does not discuss" in check.reason

    def test_contradicting_source_is_contradicted(self):
        check = assess_source_support(
            _note(),
            _page(
                "There is no evidence that attachment styles affect adult "
                "relationships in the general population."
            ),
        )
        assert check.status == "contradicted"
        assert "no evidence" in (check.excerpt or "")

    def test_missing_number_is_unsupported(self):
        check = assess_source_support(
            _note(
                claim="97% of adults experience anxious attachment.",
                source_title="Smith 2020",
            ),
            _page(
                "Smith (2020) reports that many adults experience anxious "
                "attachment in romantic relationships."
            ),
        )
        assert check.status == "unsupported"
        assert "number" in check.reason
        assert "97" in check.reason

    def test_number_spelled_as_percent_word_is_found(self):
        check = assess_source_support(
            _note(claim="97% of adults show an attachment style."),
            _page(
                "Hazan and Shaver (1987) reported that 97 percent of adults "
                "show an attachment style."
            ),
        )
        assert check.status == "supported"

    def test_number_spelled_as_decimal_fraction_is_found(self):
        check = assess_source_support(
            _note(claim="97% of adults show an attachment style."),
            _page(
                "Hazan and Shaver (1987): 0.97 of adults show an attachment "
                "style in relationships."
            ),
        )
        assert check.status == "supported"

    def test_mismatched_paper_title_is_unsupported(self):
        check = assess_source_support(
            _note(source_title="Fabricated 2021 Study on Subliminal Manifestation"),
            _page(
                "Attachment styles affect adult relationships in many people, "
                "according to general writing about feelings."
            ),
        )
        assert check.status == "unsupported"
        assert "source title not corroborated" in check.reason

    def test_empty_content_is_unverified(self):
        check = assess_source_support(_note(), _page("   "))
        assert check.status == "unverified"
        assert check.reason == "empty content"


class _FakeVerifier(ContentExtractor):
    def __init__(self, body: str, title: str = "V"):
        self._body = body
        self._title = title

    async def extract(self, urls: list[str]) -> list[ExtractedPage]:
        return [_page(self._body, self._title)]

    async def health_check(self) -> bool:
        return True


class TestVerifyEvidenceSources:
    async def test_unsupported_is_softened_and_excerpt_saved(self):
        notes = await verify_evidence_sources(
            [_note()],
            _FakeVerifier("Welcome to our homepage. Contact us for details."),
        )
        assert notes[0].usage == "soften"
        assert notes[0].confidence == "low"
        assert notes[0].verification_status == "unsupported"
        assert "source_unsupported" in (notes[0].note or "")

    async def test_contradicted_is_avoided(self):
        notes = await verify_evidence_sources(
            [_note()],
            _FakeVerifier(
                "There is no evidence that attachment styles affect adult "
                "relationships."
            ),
        )
        assert notes[0].usage == "avoid"
        assert notes[0].verification_status == "contradicted"
        assert "source_contradicted" in (notes[0].note or "")

    async def test_supported_keeps_confidence_and_records_verdict(self):
        notes = await verify_evidence_sources(
            [_note()],
            _FakeVerifier(
                "Hazan and Shaver (1987) showed attachment styles affect "
                "adult relationships."
            ),
        )
        assert notes[0].usage == "supported"
        assert notes[0].confidence == "high"
        assert notes[0].verification_status == "supported"
        assert notes[0].supporting_excerpt

    async def test_unreachable_is_unverified(self):
        class _Raising(ContentExtractor):
            async def extract(self, urls):
                raise PipelineError.__new__(PipelineError)  # any exception

            async def health_check(self) -> bool:
                return True

        notes = await verify_evidence_sources([_note()], _Raising())
        assert notes[0].usage == "avoid"
        assert notes[0].verification_status == "unverified"
        assert "source_unverified" in (notes[0].note or "")
