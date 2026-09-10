"""Evidence source verification (SEO-AUTO-DEV-SPEC.md sections 18, 25;
content guideline section 28: cited research must be verified to exist).

The LLM proposes evidence notes (claim + source URL + confidence). Before
those notes are trusted by the writer / fact reviewer, each note's source
URL is checked through an *independent* fetch channel (the content
extractor — not the same LLM that produced the note).

Audit R-H06 made this verification *substantive*: the first fix only proved
that some page answered with non-empty text, so a fabricated paper/number
paired with a live "Welcome to our homepage" still shipped as
``high / supported``. The checker now compares the fetched body against the
proposed note:

1. **title corroboration** — a significant token (or year) of
   ``source_title`` must appear on the page;
2. **number corroboration** — every statistic quoted in the ``claim``
   (``97%``, ``1,000``, ``12.5``) must actually occur in the body, in one of
   its common spellings (``97 %`` / ``97 percent`` / ``0.97``);
3. **term overlap** — enough of the claim's significant terms must occur in
   the body at all (a reachable but irrelevant page fails);
4. **contradiction** — a negation marker (``no evidence``, ``debunked``,
   ``not supported`` …) in a sentence that also carries the claim's terms
   means the source argues *against* the claim.

The verdict plus the best corroborating (or contradicting) excerpt is
persisted on the note, so the decision is auditable. Unsupported notes are
downgraded (``soften`` for unsupported, ``avoid`` for contradicted /
unverified) and the writer's context carries the excerpt.

This is a deterministic, mock-friendly contract: the orchestrator passes
the configured content extractor; unit tests pass a fake (or ``None`` to
skip).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal

from app.schemas.research import EvidenceNote

if TYPE_CHECKING:  # pragma: no cover
    from app.providers.extractor.base import ContentExtractor
    from app.schemas.sources import ExtractedPage

logger = logging.getLogger(__name__)

SupportStatus = Literal["supported", "unsupported", "contradicted", "unverified"]

#: Minimum share of a claim's significant terms that must occur in the body
#: before the page counts as on-topic at all.
MIN_TERM_OVERLAP = 0.25

#: An excerpt is a debugging artefact, not the archive — keep it short.
MAX_EXCERPT_CHARS = 400

#: Words that appear in almost every citation and therefore do not
#: corroborate a specific source title.
_GENERIC_TITLE_TOKENS = frozenset(
    {
        "study",
        "studies",
        "research",
        "researchers",
        "journal",
        "review",
        "report",
        "article",
        "paper",
        "university",
        "college",
        "institute",
        "institution",
        "foundation",
        "association",
        "society",
        "national",
        "international",
        "american",
        "press",
        "publishing",
        "science",
        "scientific",
        "psychology",
        "psychological",
        "health",
        "medicine",
        "medical",
        "analysis",
        "survey",
        "data",
        "findings",
        "center",
        "centre",
        "department",
    }
)

_STOPWORDS = frozenset(
    """the a an and or of to in on for with that this these those from by as at
    is are was were be been being it its their they you your we our can could
    should would may might will shall do does did not no than then also such
    more most other into over under about between during before after above
    below up down out off again further once here there when where why how all
    any both each few nor only own same so too very just now has have had
    than which who whom whose while because about across against among""".split()
)

#: Multi-word (or unambiguous) phrases that indicate the source refutes the
#: claim. Deliberately conservative: bare "false"/"no" would false-positive.
_NEGATION_MARKERS = (
    "no evidence",
    "no scientific evidence",
    "no reliable evidence",
    "no proof",
    "no data",
    "not supported",
    "unsupported by",
    "does not support",
    "do not support",
    "cannot be supported",
    "there is no",
    "myth",
    "debunk",
    "disproven",
    "disproves",
    "refute",
    "refutes",
    "contradicts",
    "contrary to",
    "not true",
    "is false",
    "is a misconception",
    "misconception",
)

_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:%|percent)?", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_WORD_RE = re.compile(r"[a-z][a-z0-9'\-]+")


@dataclass(frozen=True)
class SupportCheck:
    """Outcome of comparing one note against its fetched source body."""

    status: SupportStatus
    reason: str
    excerpt: str | None = None


def _normalize(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _significant_terms(text: str) -> list[str]:
    words = _WORD_RE.findall((text or "").lower())
    seen: list[str] = []
    for word in words:
        if len(word) >= 4 and word not in _STOPWORDS and word not in seen:
            seen.append(word)
    return seen


def _number_variants(raw: str) -> set[str]:
    """Spellings under which a claimed number may appear in the body."""
    match = re.match(r"^(\d[\d,]*(?:\.\d+)?)\s*(%|percent)?$", raw.strip(), re.I)
    if not match:
        return {raw.strip().lower()}
    digits = match.group(1).replace(",", "")
    variants = {raw.strip().lower(), digits}
    if match.group(2):
        variants |= {
            f"{digits}%",
            f"{digits} %",
            f"{digits} percent",
            f"{digits} per cent",
        }
        try:
            fraction = float(digits) / 100.0
            variants.add(f"{fraction:g}")
            variants.add(f"{fraction:.2f}")
        except ValueError:  # pragma: no cover - regex guarantees numeric
            pass
    return {variant.lower() for variant in variants}


def _claimed_numbers(claim: str) -> list[str]:
    return [m.group(0).strip() for m in _NUMBER_RE.finditer(claim or "")]


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?;])\s+|\n+", text)
    return [p.strip() for p in parts if p.strip()]


def _best_excerpt(sentences: list[str], terms: list[str]) -> str | None:
    best: tuple[int, str] | None = None
    for sentence in sentences:
        score = sum(1 for term in terms if term in sentence)
        if score and (best is None or score > best[0]):
            best = (score, sentence)
    if best is None:
        return None
    excerpt = best[1]
    if len(excerpt) > MAX_EXCERPT_CHARS:
        excerpt = excerpt[:MAX_EXCERPT_CHARS] + "..."
    return excerpt


def _contradicting_excerpt(sentences: list[str], terms: list[str]) -> str | None:
    for sentence in sentences:
        if any(marker in sentence for marker in _NEGATION_MARKERS) and (
            not terms or any(term in sentence for term in terms)
        ):
            excerpt = sentence
            if len(excerpt) > MAX_EXCERPT_CHARS:
                excerpt = excerpt[:MAX_EXCERPT_CHARS] + "..."
            return excerpt
    return None


def _title_matches(source_title: str, haystack: str) -> tuple[bool, str]:
    """At least one non-generic token / year of the title must be on the page."""
    tokens = [
        token
        for token in _significant_terms(source_title)
        if token not in _GENERIC_TITLE_TOKENS
    ]
    years = _YEAR_RE.findall(source_title or "")
    if not tokens and not years:
        return True, "no distinctive source title"
    if any(token in haystack for token in tokens):
        return True, "ok"
    if any(year in haystack for year in years):
        return True, "ok"
    return False, f"source title not corroborated: {source_title!r}"


def assess_source_support(note: EvidenceNote, page: "ExtractedPage") -> SupportCheck:
    """Compare one fetched page against the note's title / claim / numbers.

    Pure function (no I/O) so the rules are trivially unit-testable.
    """
    content = _normalize(page.content_markdown)
    if not content:
        return SupportCheck("unverified", "empty content")

    page_title = _normalize(page.title)
    haystack = f"{page_title} {content}".strip()
    sentences = _sentences(content)

    terms = _significant_terms(note.claim)
    title_ok, title_reason = _title_matches(note.source_title or "", haystack)

    # 1. the source must not argue against the claim
    contradicting = _contradicting_excerpt(sentences, terms)
    if contradicting is not None:
        return SupportCheck(
            "contradicted", "source contradicts the claim", contradicting
        )

    # 2. the cited source itself must be recognisable on the page
    if not title_ok:
        return SupportCheck(
            "unsupported",
            title_reason,
            _best_excerpt(sentences, terms) or content[:MAX_EXCERPT_CHARS],
        )

    # 3. every quoted statistic must actually occur in the body
    missing = [
        number
        for number in _claimed_numbers(note.claim)
        if not any(variant in content for variant in _number_variants(number))
    ]
    if missing:
        return SupportCheck(
            "unsupported",
            "claimed number(s) not found in source: " + ", ".join(missing),
            _best_excerpt(sentences, terms) or content[:MAX_EXCERPT_CHARS],
        )

    # 4. the page must be about the claim at all
    if terms:
        matched = [term for term in terms if term in content]
        overlap = len(matched) / len(terms)
        if overlap < MIN_TERM_OVERLAP:
            return SupportCheck(
                "unsupported",
                f"source does not discuss the claim (term overlap {overlap:.2f})",
                _best_excerpt(sentences, terms),
            )

    return SupportCheck(
        "supported", "ok", _best_excerpt(sentences, terms)
    )


def _append_reason(existing: str | None, addition: str) -> str:
    if existing:
        return f"{existing}; {addition}"
    return addition


#: status -> (confidence, usage, note marker)
_DOWNGRADES: dict[SupportStatus, tuple[str, str, str]] = {
    "unsupported": ("low", "soften", "source_unsupported"),
    "contradicted": ("low", "avoid", "source_contradicted"),
    "unverified": ("low", "avoid", "source_unverified"),
}


async def verify_evidence_sources(
    notes: list[EvidenceNote],
    verifier: "ContentExtractor | None",
    *,
    on_extraction: "Callable[[ExtractedPage], None] | None" = None,
) -> list[EvidenceNote]:
    """Return the notes with their source support assessed.

    When ``verifier`` is ``None`` the notes pass through unchanged (no
    provider wired, e.g. focused unit tests). Otherwise each note's
    ``source_url`` is fetched and the body is compared against the note:

    * ``supported``        -> unchanged (``verification_status`` recorded);
    * ``unsupported``      -> ``confidence="low"``, ``usage="soften"``;
    * ``contradicted``     -> ``confidence="low"``, ``usage="avoid"``;
    * ``unverified`` (missing / unreachable / empty source)
                           -> ``confidence="low"``, ``usage="avoid"``.

    Every outcome stores ``verification_status`` and the corroborating /
    contradicting ``supporting_excerpt`` (R-H06), so a later reviewer can
    audit *why* a note was trusted.
    """
    if verifier is None:
        return list(notes)

    verified: list[EvidenceNote] = []
    for note in notes:
        check = await _verify_one(verifier, note, on_extraction=on_extraction)
        if check.status == "supported":
            verified.append(
                note.model_copy(
                    update={
                        "verification_status": check.status,
                        "supporting_excerpt": check.excerpt,
                    }
                )
            )
            continue

        confidence, usage, marker = _DOWNGRADES[check.status]
        logger.warning(
            "evidence_source_downgraded",
            extra={
                "event": "evidence_source_downgraded",
                "source_url": note.source_url,
                "verification_status": check.status,
                "reason": check.reason,
            },
        )
        verified.append(
            note.model_copy(
                update={
                    "confidence": confidence,
                    "usage": usage,
                    "note": _append_reason(
                        note.note, f"{marker}: {check.reason}"
                    ),
                    "verification_status": check.status,
                    "supporting_excerpt": check.excerpt,
                }
            )
        )
    return verified


async def _verify_one(
    verifier: "ContentExtractor",
    note: EvidenceNote,
    *,
    on_extraction: "Callable[[ExtractedPage], None] | None" = None,
) -> SupportCheck:
    """Fetch the note's source URL and assess support for the claim.

    ``on_extraction`` (R-M02) is invoked for every fetched page so the caller
    can record the independent verification fetch in the cost ledger — this
    extraction is a real paid call that the per-artifact columns never saw.
    """
    if not note.source_url:
        return SupportCheck("unverified", "missing source_url")
    try:
        pages = await verifier.extract([note.source_url])
    except Exception as exc:  # noqa: BLE001 — any fetch failure means "unverified"
        return SupportCheck("unverified", f"fetch failed: {exc.__class__.__name__}")
    if not pages:
        return SupportCheck("unverified", "no content returned")
    if on_extraction is not None:
        for page in pages:
            on_extraction(page)
    page = pages[0]
    if not (page.content_markdown or "").strip():
        return SupportCheck("unverified", "empty content")
    return assess_source_support(note, page)
