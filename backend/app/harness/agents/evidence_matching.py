from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable


_TYPOGRAPHIC_EQUIVALENTS = {
    ".": ".。",
    "。": ".。",
    ",": ",，",
    "，": ",，",
    ":": ":：",
    "：": ":：",
    ";": ";；",
    "；": ";；",
    "!": "!！",
    "！": "!！",
    "?": "?？",
    "？": "?？",
    '"': '"“”',
    "“": '"“”',
    "”": '"“”',
    "'": "'‘’",
    "‘": "'‘’",
    "’": "'‘’",
}
_QUOTE_BOUNDARIES = frozenset('"“”\'‘’')
_SENTENCE_BOUNDARY = re.compile(r"[^\n。！？!?]+[。！？!?]?|[^\n]+")


def resolve_verbatim_evidence_quote(draft: str, quote: str) -> str | None:
    """Return the unique draft substring for a typography-only quote variant."""

    if quote in draft:
        return quote
    if not quote:
        return None

    resolved = _resolve_unique_typographic_match(draft, quote)
    if resolved is not None:
        return resolved

    boundary_variants: list[str] = []
    if quote[0] in _QUOTE_BOUNDARIES:
        boundary_variants.append(quote[1:])
    if quote[-1] in _QUOTE_BOUNDARIES:
        boundary_variants.append(quote[:-1])
    if quote[0] in _QUOTE_BOUNDARIES and quote[-1] in _QUOTE_BOUNDARIES:
        boundary_variants.append(quote[1:-1])
    for variant in boundary_variants:
        if not variant:
            continue
        resolved = _resolve_unique_typographic_match(draft, variant)
        if resolved is not None:
            return resolved
    return None


def resolve_semantic_evidence_quote(
    draft: str,
    hints: Iterable[str],
) -> str | None:
    """Bind semantic evidence hints to one exact draft span.

    The provider supplies meaning, never a verbatim authorization token.  This
    deterministic binder owns sentence segmentation, exact quote materialization,
    and ambiguity handling.  A weak semantic match returns ``None`` so the caller
    can report an unsupported fact instead of asking the model to transcribe text.
    """

    semantic_hints = [item.strip() for item in hints if item and item.strip()]
    if not draft.strip() or not semantic_hints:
        return None
    spans = _evidence_spans(draft)
    if not spans:
        return None

    for hint in semantic_hints:
        if hint in draft:
            containing = [span for span in spans if hint in span]
            if containing:
                return min(containing, key=len)

    ranked = sorted(
        (
            (
                max(_semantic_similarity(hint, span) for hint in semantic_hints),
                index,
                span,
            )
            for index, span in enumerate(spans)
        ),
        key=lambda item: (-item[0], item[1]),
    )
    best_score, _, best_span = ranked[0]
    if best_score < 0.18:
        return None
    if len(ranked) > 1 and abs(best_score - ranked[1][0]) < 0.015:
        return None
    return best_span


def materialize_semantic_evidence_quote(
    draft: str,
    hints: Iterable[str],
) -> str | None:
    """Materialize one exact span without treating lexical ambiguity as semantics.

    Candidate meaning is reviewed by the Evaluator.  This helper only performs the
    Harness-owned mechanical step of choosing a stable verbatim span, so a tie or a
    weak lexical score must not send the writing model into a prose-rewrite loop.
    """

    semantic_hints = [item.strip() for item in hints if item and item.strip()]
    if not draft.strip() or not semantic_hints:
        return None
    spans = _evidence_spans(draft)
    if not spans:
        return None

    for hint in semantic_hints:
        if hint in draft:
            containing = [span for span in spans if hint in span]
            if containing:
                return min(containing, key=len)

    return max(
        enumerate(spans),
        key=lambda item: (
            max(_semantic_similarity(hint, item[1]) for hint in semantic_hints),
            -item[0],
        ),
    )[1]


def resolve_semantic_choice(
    hint: str,
    choices: dict[str, Iterable[str]],
) -> str | None:
    """Resolve a semantic label to one Harness-owned key without key transcription."""

    query = hint.strip()
    if not query or not choices:
        return None
    ranked: list[tuple[float, str]] = []
    normalized_query = _normalize_semantic_text(query)
    for key, labels in choices.items():
        candidates = [key, *(label for label in labels if label)]
        normalized = [_normalize_semantic_text(item) for item in candidates]
        if normalized_query in normalized:
            ranked.append((1.0, key))
            continue
        ranked.append(
            (max(_semantic_similarity(query, item) for item in candidates), key)
        )
    ranked.sort(key=lambda item: (-item[0], item[1]))
    best_score = ranked[0][0]
    if best_score < 0.12:
        return None
    if len(ranked) > 1:
        second_score = ranked[1][0]
        gap = best_score - second_score
        if gap < 0.03:
            return None
        if best_score < 0.35 and not (
            gap >= 0.08
            and (second_score == 0 or best_score >= second_score * 2)
        ):
            return None
    return ranked[0][1]


def _resolve_unique_typographic_match(draft: str, quote: str) -> str | None:
    pattern_parts: list[str] = []
    index = 0
    while index < len(quote):
        char = quote[index]
        if char.isspace():
            while index + 1 < len(quote) and quote[index + 1].isspace():
                index += 1
            pattern_parts.append(r"\s+")
        elif char in _TYPOGRAPHIC_EQUIVALENTS:
            pattern_parts.append(
                "[" + re.escape(_TYPOGRAPHIC_EQUIVALENTS[char]) + "]"
            )
        else:
            pattern_parts.append(re.escape(char))
        index += 1

    matches = list(re.finditer("".join(pattern_parts), draft))
    if len(matches) != 1:
        return None
    return matches[0].group(0)


def _evidence_spans(draft: str) -> list[str]:
    spans: list[str] = []
    for paragraph in draft.splitlines():
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        for match in _SENTENCE_BOUNDARY.finditer(paragraph):
            span = match.group(0).strip()
            if span:
                spans.append(span)
    return spans


def _semantic_similarity(left: str, right: str) -> float:
    normalized_left = _normalize_semantic_text(left)
    normalized_right = _normalize_semantic_text(right)
    if not normalized_left or not normalized_right:
        return 0.0
    if normalized_left in normalized_right or normalized_right in normalized_left:
        shorter = min(len(normalized_left), len(normalized_right))
        longer = max(len(normalized_left), len(normalized_right))
        return 0.75 + 0.25 * (shorter / longer)
    left_tokens = _semantic_tokens(normalized_left)
    right_tokens = _semantic_tokens(normalized_right)
    overlap = len(left_tokens & right_tokens)
    coverage = overlap / max(len(left_tokens), 1)
    precision = overlap / max(len(right_tokens), 1)
    sequence = SequenceMatcher(None, normalized_left, normalized_right).ratio()
    return 0.5 * coverage + 0.2 * precision + 0.3 * sequence


def _normalize_semantic_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(char for char in normalized if char.isalnum())


def _semantic_tokens(value: str) -> set[str]:
    if not value:
        return set()
    tokens = {value[index : index + 2] for index in range(max(len(value) - 1, 0))}
    if len(value) == 1:
        tokens.add(value)
    return tokens
