"""Programmatic anti-copy check (SEO-AUTO-DEV-SPEC.md section 29).

V1 algorithm:

1. Split the draft into sentences.
2. Compare each sentence with every competitor source.
3. RapidFuzz (with difflib SequenceMatcher fallback) similarity.
4. Flag long near-exact fragments as ``possible_source_overlap``.

Suggested thresholds (section 29):

* >= 12 words exact/near-exact match, OR
* similarity >= 0.85.

The report is a list of flags for the Reviser — NOT a pass/fail
"duplication rate".
"""

import re

try:  # rapidfuzz is preferred (spec section 29)
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover - dependency is declared
    fuzz = None
    import difflib


#: Minimum sentence length (words) worth comparing.
MIN_SENTENCE_WORDS = 8
#: Section 29: >= 12 words exact/near-exact.
MIN_OVERLAP_WORDS = 12
#: Section 29: similarity >= 0.85.
MIN_SIMILARITY = 0.85

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _token_count(text: str) -> int:
    return len(text.split())


def _similarity(a: str, b: str) -> float:
    """Sentence-to-source similarity in [0, 1].

    ``fuzz.ratio`` is diluted by length difference, so we use the
    sentence's best alignment over the source: ratio against
    equal-length windows is the classic plagiarism-style check and
    matches SequenceMatcher's ``find_longest_match`` semantics.
    """
    a_norm = a.lower().strip()
    b_norm = b.lower().strip()
    if not a_norm or not b_norm:
        return 0.0
    if len(b_norm) <= len(a_norm):
        raw = fuzz.ratio(a_norm, b_norm) if fuzz else difflib.SequenceMatcher(None, a_norm, b_norm).ratio()
        return raw / 100.0 if fuzz else raw

    window = len(a_norm)
    best = 0.0
    step = max(window // 4, 1)
    for i in range(0, len(b_norm) - window + 1, step):
        cand = b_norm[i : i + window]
        raw = fuzz.ratio(a_norm, cand) if fuzz else difflib.SequenceMatcher(None, a_norm, cand).ratio()
        score = raw / 100.0 if fuzz else raw
        if score > best:
            best = score
            if best >= 1.0:
                break
    return best


def _best_window(a: str, b: str, step: int) -> str:
    """Return the best-aligned substring of ``b`` for diagnostics."""
    window = len(a)
    if len(b) <= window:
        return b
    best, best_score = b[0:window], -1.0
    for i in range(0, len(b) - window + 1, step):
        cand = b[i : i + window]
        raw = fuzz.ratio(a, cand) if fuzz else 0.0
        score = raw / 100.0 if fuzz else raw
        if score > best_score:
            best, best_score = cand, score
    return best


def _split_sentences(markdown: str) -> list[str]:
    """Split a Markdown body into sentence-like fragments.

    Blank lines first (headings/paragraphs must not merge into one
    "sentence"), then terminal punctuation.
    """
    parts = re.split(r"\n\s*\n", markdown)
    sentences: list[str] = []
    for part in parts:
        for s in _SENTENCE_SPLIT.split(part):
            s = s.strip()
            if s:
                sentences.append(s)
    return sentences


def check_anti_copy(
    body_markdown: str,
    competitor_texts: list[tuple[str, str]],
    *,
    min_words: int = MIN_OVERLAP_WORDS,
    min_similarity: float = MIN_SIMILARITY,
) -> dict:
    """Run the anti-copy check.

    Args:
        body_markdown: the draft body (H1-free Markdown).
        competitor_texts: list of ``(source_url, content_markdown)`` —
            the full competitor sources (they are never sent to the
            LLM; only compared here programmatically, section 29).
        min_words / min_similarity: section 29 thresholds.

    Returns:
        A dict matching ``AntiCopyReport``.
    """
    from app.schemas.article import AntiCopyMatch

    matches: list[AntiCopyMatch] = []
    sentences = [
        s for s in _split_sentences(body_markdown)
        if _token_count(s) >= MIN_SENTENCE_WORDS
    ]

    for url, content in competitor_texts:
        content = (content or "").strip()
        if not content:
            continue
        for sentence in sentences:
            sim = _similarity(sentence, content)
            if sim < min_similarity:
                continue
            if _token_count(sentence) < min_words:
                continue
            # Diagnostics: the source fragment the sentence aligns with.
            step = max(len(sentence) // 4, 1)
            matched = _best_window(sentence, content, step)
            matches.append(
                AntiCopyMatch(
                    draft_phrase=sentence,
                    source_url=url,
                    matched_phrase=matched[:300],
                    similarity=round(sim, 3),
                    word_count=_token_count(sentence),
                )
            )

    return {
        "matches": [m.model_dump() for m in matches],
        "sources_compared": len(competitor_texts),
        "has_serious_overlap": bool(matches),
    }
