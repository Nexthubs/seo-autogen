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

1. **title corroboration** — the year (when supplied) and a substantial
   share of the distinctive title/author tokens must appear on the page;
2. **number corroboration** — every statistic quoted in the ``claim``
   (``97%``, ``1,000``, ``12.5``) must actually occur in the body, in one of
   its common spellings (``97 %`` / ``97 percent`` / ``0.97``);
3. **sentence-level support** — one sentence must carry most of the claim's
   significant terms; page-wide word overlap is screening evidence only;
4. **contradiction** — a relevant sentence with a negation marker or an
   opposite directional predicate (for example reduces vs increases) means
   the source argues *against* the claim.

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
import math
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
MIN_TERM_OVERLAP = 0.60
MIN_ORDERED_CLAIM_COVERAGE = 0.75

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

_DIRECTION_GROUPS = (
    (frozenset({"increase", "raise", "higher", "grow"}),
     frozenset({"decrease", "reduce", "lower", "decline"})),
    (frozenset({"improve", "benefit", "better"}),
     frozenset({"worsen", "harm", "worse"})),
    (frozenset({"positive", "positively"}),
     frozenset({"negative", "negatively"})),
    (frozenset({"more", "greater"}), frozenset({"less", "fewer"})),
)


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


def _stem(word: str) -> str:
    """Small deterministic stemmer for matching ordinary English variants."""
    word = word.lower()
    for suffix in ("ingly", "edly", "ing", "ies", "ed", "es", "s"):
        if len(word) - len(suffix) >= 4 and word.endswith(suffix):
            word = word[:-len(suffix)] + ("y" if suffix == "ies" else "")
            break
    # reduce/reduces and increase/increases should share a stem.
    if len(word) > 4 and word.endswith("e"):
        word = word[:-1]
    return word


def _term_stems(text: str) -> set[str]:
    return {_stem(term) for term in _significant_terms(text)}


def _number_present(raw: str, text: str) -> bool:
    """Match a number as a numeric token with its claimed unit.

    In particular, a percentage never degrades to a naked digit substring:
    ``97%`` cannot be corroborated by the year/population ``1970``.
    """
    match = re.match(r"^(\d[\d,]*(?:\.\d+)?)\s*(%|percent)?$", raw.strip(), re.I)
    if not match:
        return False
    digits = match.group(1).replace(",", "")
    escaped = re.escape(digits)
    if match.group(2):
        percent = re.compile(
            rf"(?<![\d.]){escaped}(?:\.0+)?\s*(?:%|percent\b|per\s+cent\b)",
            re.I,
        )
        try:
            fraction = float(digits) / 100.0
        except ValueError:  # pragma: no cover - regex guarantees numeric
            return bool(percent.search(text))
        fraction_text = f"{fraction:g}"
        fraction_pattern = re.compile(
            rf"(?<![\d.]){re.escape(fraction_text)}(?![\d.])"
            r"\s+(?:of\b|as\s+a\s+(?:fraction|proportion)\b)"
        )
        return bool(percent.search(text) or fraction_pattern.search(text))

    compact = text.replace(",", "")
    return bool(re.search(rf"(?<![\d.]){escaped}(?![\d.])", compact))


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


def _sentence_coverage(sentence: str, claim_stems: set[str]) -> float:
    if not claim_stems:
        return 1.0
    return len(claim_stems & _term_stems(sentence)) / len(claim_stems)


def _ordered_claim_coverage(claim: str, sentence: str) -> float:
    """LCS coverage for a recognizable, ordered statement of the claim."""
    claim_terms = [_stem(term) for term in _significant_terms(claim)]
    sentence_terms = [_stem(term) for term in _significant_terms(sentence)]
    if not claim_terms:
        return 1.0
    previous = [0] * (len(sentence_terms) + 1)
    for claim_term in claim_terms:
        current = [0]
        for index, sentence_term in enumerate(sentence_terms, start=1):
            if claim_term == sentence_term:
                current.append(previous[index - 1] + 1)
            else:
                current.append(max(current[-1], previous[index]))
        previous = current
    return previous[-1] / len(claim_terms)


def _opposite_direction(claim: str, sentence: str) -> bool:
    claim_stems = _term_stems(claim)
    sentence_stems = _term_stems(sentence)
    for left, right in _DIRECTION_GROUPS:
        left_stems = {_stem(word) for word in left}
        right_stems = {_stem(word) for word in right}
        if claim_stems & left_stems and sentence_stems & right_stems:
            return True
        if claim_stems & right_stems and sentence_stems & left_stems:
            return True
    return False


def _contradicting_excerpt(
    sentences: list[str], terms: list[str], claim: str
) -> str | None:
    claim_stems = {_stem(term) for term in terms}
    for sentence in sentences:
        coverage = _sentence_coverage(sentence, claim_stems)
        relevant = coverage >= MIN_TERM_OVERLAP
        if relevant and (
            any(marker in sentence for marker in _NEGATION_MARKERS)
            or _opposite_direction(claim, sentence)
        ):
            excerpt = sentence
            if len(excerpt) > MAX_EXCERPT_CHARS:
                excerpt = excerpt[:MAX_EXCERPT_CHARS] + "..."
            return excerpt
    return None


def _title_matches(source_title: str, haystack: str) -> tuple[bool, str]:
    """Require a recognizable title/author identity, not one shared word."""
    tokens = [
        token
        for token in _significant_terms(source_title)
        if token not in _GENERIC_TITLE_TOKENS
    ]
    years = _YEAR_RE.findall(source_title or "")
    if not tokens and not years:
        return True, "no distinctive source title"
    haystack_stems = _term_stems(haystack)
    matched_tokens = sum(1 for token in tokens if _stem(token) in haystack_stems)
    required_tokens = min(len(tokens), max(1, math.ceil(len(tokens) * 0.80)))
    years_ok = all(re.search(rf"\b{re.escape(year)}\b", haystack) for year in years)
    if matched_tokens >= required_tokens and years_ok:
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
    claim_stems = {_stem(term) for term in terms}
    title_ok, title_reason = _title_matches(note.source_title or "", haystack)

    # 1. the source must not argue against the claim
    contradicting = _contradicting_excerpt(sentences, terms, note.claim)
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

    # 3. support must occur in one claim-relevant sentence. Page-wide term
    # overlap is not enough to prove that the page makes the asserted claim.
    ranked = sorted(
        (
            (
                _sentence_coverage(sentence, claim_stems),
                _ordered_claim_coverage(note.claim, sentence),
                sentence,
            )
            for sentence in sentences
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    best_coverage, ordered_coverage, support_sentence = (
        ranked[0] if ranked else (0.0, 0.0, "")
    )
    if terms and (
        best_coverage < MIN_TERM_OVERLAP
        or ordered_coverage < MIN_ORDERED_CLAIM_COVERAGE
    ):
        return SupportCheck(
            "unsupported",
            "source does not state the claim "
            f"(sentence overlap {best_coverage:.2f}, "
            f"ordered claim coverage {ordered_coverage:.2f})",
            _best_excerpt(sentences, terms),
        )

    # 4. every quoted statistic must occur as a token with the same unit in
    # that claim-relevant sentence, rather than somewhere unrelated on-page.
    missing = [
        number
        for number in _claimed_numbers(note.claim)
        if not _number_present(number, support_sentence)
    ]
    if missing:
        return SupportCheck(
            "unsupported",
            "claimed number(s) not found in source: " + ", ".join(missing),
            _best_excerpt(sentences, terms) or content[:MAX_EXCERPT_CHARS],
        )

    return SupportCheck(
        "supported", "ok", support_sentence[:MAX_EXCERPT_CHARS] or None
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
