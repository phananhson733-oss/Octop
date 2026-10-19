"""Brand mentions and citations in AI answers.

Ported from OpenSEO d7ea785 features/ai-visibility/services/aiVisibilityMatching.ts (MIT).
Word boundaries are checked by hand: stdlib ``re`` has no ``\\p{L}`` classes.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from typing import Any

# URLs and emails in answer prose are not brand mentions. They are blanked keeping their
# length, so mention offsets still address the original answer.
_LINKS = re.compile(r"(?:https?://|www\.)[^\s<>]+|[\w.%+-]+@[\w.-]+\.[^\W\d_]{2,}", re.IGNORECASE)
# Two-label public suffixes in the project markets (US, UK, Canada, Australia).
_SECOND_LEVEL = {
    "co.uk",
    "org.uk",
    "me.uk",
    "ltd.uk",
    "plc.uk",
    "ac.uk",
    "gov.uk",
    "com.au",
    "net.au",
    "org.au",
    "edu.au",
    "gov.au",
}


def _word(char: str) -> bool:
    return char == "_" or unicodedata.category(char)[0] in "LMN"


def _find(
    text: str,
    pattern: re.Pattern[str],
    before: Callable[[str], bool] = _word,
    after: Callable[[str], bool] = _word,
) -> list[tuple[int, int]]:
    """Matches of ``pattern`` not touching a character ``before``/``after`` reject."""
    spans: list[tuple[int, int]] = []
    pos = 0
    while (match := pattern.search(text, pos)) is not None:
        start, end = match.span()
        if (start == 0 or not before(text[start - 1])) and (
            end == len(text) or not after(text[end])
        ):
            spans.append((start, end))
            pos = end
        else:
            pos = start + 1
    return spans


def mention_spans(text: str, names: list[str]) -> list[tuple[int, int]]:
    """Where any of ``names`` appears as whole words, outermost spans first."""
    prose = _LINKS.sub(lambda m: " " * len(m.group()), text)
    # Match canonically equivalent Unicode, mapping back to the original offsets.
    chars: list[str] = []
    starts: list[int] = []
    for offset, char in enumerate(prose):
        for part in unicodedata.normalize("NFD", char):
            chars.append(part)
            starts.append(offset)
    normalized = "".join(chars)
    found: set[tuple[int, int]] = set()
    for name in dict.fromkeys(name.strip() for name in names):
        if len(name) < 2:
            continue
        words = unicodedata.normalize("NFD", name).split()
        pattern = re.compile(r"\s+".join(re.escape(word) for word in words), re.IGNORECASE)
        for start, end in _find(normalized, pattern):
            found.add((starts[start], starts[end - 1] + 1))
    ordered = sorted(found, key=lambda span: (span[0], -span[1]))
    return [
        span
        for i, span in enumerate(ordered)
        if not any(o[0] <= span[0] and o[1] >= span[1] for o in ordered[:i])
    ]


def _root_label(value: str) -> str | None:
    """``www.acme.co.uk`` -> ``acme``; subdomains and non-domains -> ``None``."""
    host = value.strip().lower().removeprefix("www.")
    if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host):
        return None
    labels = host.split(".")
    size = 3 if ".".join(labels[-2:]) in _SECOND_LEVEL else 2
    return labels[0] if len(labels) == size else None


def brand_names(brand: dict[str, Any]) -> list[str]:
    """Answer text naming the brand. A brand named "acme.com" is written "Acme"."""
    stem = _root_label(brand["name"])
    if stem and stem == _root_label(brand["domain"]):
        return [brand["name"], brand["domain"], stem]
    return [brand["name"], brand["domain"]]


def owned_domain(hostname: str, owned: str) -> bool:
    host = hostname.lower().removeprefix("www.")
    domain = owned.lower().removeprefix("www.")
    return bool(domain) and (host == domain or host.endswith("." + domain))


def match_brand(
    answer_text: str, citations: list[dict[str, Any]], brand: dict[str, Any]
) -> dict[str, Any]:
    """Mentioned when the prose names the brand, cited when a cited page is on its domain.

    A generic brand name can count as a mention of something else; accepted for simplicity.
    """
    spans = mention_spans(answer_text, brand_names(brand))
    return {
        "name": brand["name"],
        "domain": brand["domain"],
        "own": brand["own"],
        "mentioned": bool(spans),
        "cited": any(owned_domain(c["domain"], brand["domain"]) for c in citations),
        "first_mention": spans[0][0] if spans else None,
    }


def prompt_is_branded(prompt: str, own: dict[str, Any]) -> bool:
    """The prompt names the own brand, or its domain (``x.com.evil.example`` is not it)."""
    if mention_spans(prompt, [own["name"]]):
        return True
    domain = own["domain"].lower().removeprefix("www.")
    if not domain:
        return False
    return bool(
        _find(
            prompt,
            re.compile(re.escape(domain), re.IGNORECASE),
            before=lambda c: _word(c) or c == "-",
            after=lambda c: _word(c) or c in ".-",
        )
    )
