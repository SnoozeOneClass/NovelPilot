from __future__ import annotations

import re


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
