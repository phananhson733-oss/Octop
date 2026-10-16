"""JSON-LD checks: which blocks parse, which rich-result types lack required properties,
and whether FAQ markup matches the visible page.

The required properties follow Google's structured data documentation for the types
that can earn a rich result. Types outside the table are not judged, rather than
assumed complete.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

MAX_BLOCKS = 20
MAX_REPORTED = 10

# Each inner tuple is one requirement; several names mean any one of them will do.
REQUIRED: dict[str, tuple[tuple[str, ...], ...]] = {
    "Product": (("name",), ("offers", "review", "aggregateRating")),
    "Offer": (("price", "priceSpecification"),),
    "AggregateOffer": (("lowPrice",), ("priceCurrency",)),
    "AggregateRating": (("ratingValue",), ("ratingCount", "reviewCount")),
    "Review": (("author",), ("reviewRating",)),
    "BreadcrumbList": (("itemListElement",),),
    "ListItem": (("position",), ("name", "item")),
    "FAQPage": (("mainEntity",),),
    # A QAPage question may have only suggested answers. An FAQPage question needs an
    # accepted one, but nodes are judged without their parent, so either will do here.
    "Question": (("name",), ("acceptedAnswer", "suggestedAnswer")),
    "VideoObject": (("name",), ("thumbnailUrl",), ("uploadDate",)),
    "Event": (("name",), ("startDate",), ("location",)),
    "Recipe": (("name",), ("image",)),
    "JobPosting": (("title",), ("description",), ("datePosted",), ("hiringOrganization",)),
}
_SCHEMA_PREFIX = re.compile(r"^(?:https?://)?schema\.org/", re.I)
_WORD = re.compile(r"\w+")


def parse_blocks(blocks: list[str]) -> tuple[list[Any], int]:
    """Parsed JSON-LD documents and how many blocks failed to parse."""
    documents: list[Any] = []
    invalid = 0
    for block in blocks[:MAX_BLOCKS]:
        text = block.strip()
        # Some templates wrap the JSON in an HTML comment or CDATA marker.
        text = re.sub(r"^(?:<!--|<!\[CDATA\[)|(?:-->|\]\]>)$", "", text).strip()
        if not text:
            continue
        try:
            # Parsed whole: a cut block never parses, and the page is at most 1 MiB anyway.
            documents.append(json.loads(text))
        except ValueError:
            invalid += 1
    return documents, invalid


def _types(node: dict[str, Any]) -> list[str]:
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return [_SCHEMA_PREFIX.sub("", value) for value in values if isinstance(value, str)]


def _nodes(value: Any, depth: int = 0) -> Iterator[dict[str, Any]]:
    """Every object in the documents, nested ones and @graph members included."""
    if depth > 20:
        return
    if isinstance(value, list):
        for item in value:
            yield from _nodes(item, depth + 1)
    elif isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _nodes(child, depth + 1)


def _present(value: Any) -> bool:
    return value not in (None, "", [], {})


def missing_required(documents: list[Any]) -> list[str]:
    """``"Product: offers | review | aggregateRating"`` for each unmet requirement.

    Objects sharing an ``@id`` are one node: a ``{"@type": "Product", "@id": "#p"}``
    reference elsewhere in the graph is not a second, empty product.
    """
    by_id: dict[str, dict[str, Any]] = {}
    plain: list[dict[str, Any]] = []
    for node in _nodes(documents):
        node_id = node.get("@id")
        if isinstance(node_id, str) and node_id:
            by_id.setdefault(node_id, {}).update(
                {key: value for key, value in node.items() if _present(value)}
            )
        else:
            plain.append(node)
    missing: dict[str, None] = {}
    for node in [*by_id.values(), *plain]:
        for kind in _types(node):
            for names in REQUIRED.get(kind, ()):
                if not any(_present(node.get(name)) for name in names):
                    missing[f"{kind}: {' | '.join(names)}"] = None
    return list(missing)[:MAX_REPORTED]


def faq_questions(documents: list[Any]) -> list[str]:
    """The question texts FAQPage markup declares."""
    questions: list[str] = []
    for node in _nodes(documents):
        if "FAQPage" not in _types(node):
            continue
        for entity in _nodes(node.get("mainEntity")):
            name = entity.get("name")
            if "Question" in _types(entity) and isinstance(name, str) and name.strip():
                questions.append(re.sub(r"<[^>]+>", " ", name).strip())
    return list(dict.fromkeys(questions))


def _words(text: str) -> str:
    return " ".join(_WORD.findall(text.lower()))


def hidden_questions(questions: list[str], visible_text: str) -> list[str]:
    """FAQ questions whose words do not appear, in order, in the page's visible text."""
    visible = _words(visible_text)
    return [question for question in questions if _words(question) not in visible]
