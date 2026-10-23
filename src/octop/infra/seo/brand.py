"""Brand lookup shaping, ported from OpenSEO 2d8236a features/ai-search/services/
brandLookupShaping.ts, citedSources.ts and shareOfVoice.ts (MIT).

Pure functions over the LLM Mentions answers; a call that failed is ``None``.
"""

from __future__ import annotations

import math
from typing import Any
from urllib.parse import urlsplit

from octop.infra.seo.provider import LLM_PLATFORMS, safe_http_url

TOP_QUERIES_PER_PLATFORM = 25
TOP_SOURCES_PER_PLATFORM = 10
KEYWORDS_PER_SOURCE = 50
SOURCES_PER_QUERY = 10
BRANDS_PER_QUERY = 20
MAX_URL = 2048
MAX_TITLE = 300
MAX_QUESTION = 500
MAX_BRAND = 200


def _round(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(value)


def _sum(values: list[int | None]) -> int | None:
    """None when every value is unknown; unknown values never count as zero."""
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _group(groups: Any, platform: str) -> dict[str, Any]:
    return next((group for group in _list(groups) if group.get("key") == platform), {})


def _text(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _url(value: Any) -> str | None:
    url = safe_http_url(value)
    return url if url is not None and len(url) <= MAX_URL else None


def _by_volume(row: dict[str, Any], key: str) -> int:
    return row[key] if row[key] is not None else 0


def _queries(platform: str, mentions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in mentions:
        question = _text(item.get("question"), MAX_QUESTION)
        if question is None:
            continue
        sources = []
        for source in _list(item.get("sources")):
            url = _url(source.get("url"))
            if url is not None:
                title = _text(source.get("title"), MAX_TITLE)
                sources.append({"url": url, "domain": urlsplit(url).hostname, "title": title})
        brands = [
            title
            for entity in _list(item.get("brand_entities"))
            if (title := _text(entity.get("title"), MAX_BRAND)) is not None
        ]
        rows.append(
            {
                "section": "query",
                "platform": platform,
                "question": question,
                "ai_search_volume": _round(item.get("ai_search_volume")),
                "first_seen_at": _text(item.get("first_response_at"), 40),
                "last_seen_at": _text(item.get("last_response_at"), 40),
                "cited_sources": sources[:SOURCES_PER_QUERY],
                "brands_mentioned": brands[:BRANDS_PER_QUERY],
            }
        )
    rows.sort(key=lambda row: -_by_volume(row, "ai_search_volume"))
    return rows[:TOP_QUERIES_PER_PLATFORM]


def _pages(
    platform: str, top_pages: list[dict[str, Any]], mentions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """citedSources.ts: top_pages rows, with example prompts from the mentions sample."""
    examples: dict[str, dict[str, int | None]] = {}
    for item in mentions:
        question = _text(item.get("question"), MAX_QUESTION)
        if question is None:
            continue
        for source in _list(item.get("sources")):
            url = safe_http_url(source.get("url"))
            if url is not None:
                examples.setdefault(url, {}).setdefault(
                    question, _round(item.get("ai_search_volume"))
                )
    rows = []
    for page in top_pages:
        url = _url(page.get("key"))
        if url is None:
            continue
        group = _group(page.get("platform"), platform)
        keywords = [
            {"question": question, "ai_search_volume": volume}
            for question, volume in examples.get(url, {}).items()
        ]
        keywords.sort(key=lambda row: -_by_volume(row, "ai_search_volume"))
        rows.append(
            {
                "section": "page",
                "platform": platform,
                "url": url,
                "domain": urlsplit(url).hostname,
                "mentions": _round(group.get("mentions")),
                "captured_volume": _round(group.get("ai_search_volume")),
                "keywords": keywords[:KEYWORDS_PER_SOURCE],
            }
        )
    rows.sort(key=lambda row: -_by_volume(row, "captured_volume"))
    return rows[:TOP_SOURCES_PER_PLATFORM]


def _monthly(mention_sets: list[list[dict[str, Any]]]) -> list[dict[str, int]]:
    """The last 12 months of AI search volume summed over the mentioned prompts."""
    totals: dict[tuple[int, int], float] = {}
    for mentions in mention_sets:
        for item in mentions:
            for month in _list(item.get("monthly_searches")):
                year, number = month.get("year"), month.get("month")
                volume = month.get("search_volume")
                if (
                    isinstance(year, int)
                    and isinstance(number, int)
                    and 1 <= number <= 12
                    and isinstance(volume, (int, float))
                    and _round(volume) is not None
                ):
                    totals[(year, number)] = totals.get((year, number), 0) + volume
    return [
        {"year": year, "month": month, "volume": round(volume)}
        for (year, month), volume in sorted(totals.items())
    ][-12:]


def share_of_voice(
    cross: dict[str, list[dict[str, Any]] | None], target: str, competitors: list[str]
) -> dict[str, Any] | None:
    """shareOfVoice.ts computeShareOfVoice: mentions per requested brand summed over the
    platforms that answered; unknown stays unknown and is left out of the share."""
    answered = [platform for platform in LLM_PLATFORMS if cross.get(platform) is not None]
    if not competitors or not answered:
        return None
    labels = {key.lower(): key for key in [target, *competitors]}
    mentions: dict[str, int | None] = dict.fromkeys(labels)
    for platform in answered:
        for item in cross[platform] or []:
            key = item.get("key")
            if not isinstance(key, str) or key.lower() not in labels:
                continue
            found = _sum([_round(group.get("mentions")) for group in _list(item.get("platform"))])
            mentions[key.lower()] = _sum([mentions[key.lower()], found])
    total = _sum(list(mentions.values())) or 0
    ordered = sorted(mentions.items(), key=lambda pair: -(pair[1] if pair[1] is not None else -1))
    entries = [
        {
            "label": labels[key],
            "is_target": key == target.lower(),
            "mentions": count,
            "share_pct": None if count is None or total <= 0 else count / total * 100,
        }
        for key, count in ordered
    ]
    return {"platforms": answered, "entries": entries}


def shape_brand(
    calls: dict[tuple[str, str], Any],
    *,
    target: str,
    competitors: list[str],
    chatgpt_counted: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The overview and rows from each ``(platform, part)`` answer.

    Parts are ``aggregated``, ``top_pages``, ``mentions`` and ``cross``. ChatGPT data is
    US English only, so it is left out of totals, the trend and share of voice unless
    the project's market is US English too.
    """
    platforms: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    mention_sets = []
    for platform in LLM_PLATFORMS:
        parts = [calls.get((platform, part)) for part in ("aggregated", "top_pages", "mentions")]
        aggregated, top_pages, mentions = parts
        group = _group(aggregated, platform)
        platforms.append(
            {
                "platform": platform,
                "status": "failed" if all(part is None for part in parts) else "completed",
                "mentions": _round(group.get("mentions")),
                "ai_search_volume": _round(group.get("ai_search_volume")),
            }
        )
        rows += _pages(platform, top_pages or [], mentions or [])
        rows += _queries(platform, mentions or [])
        if chatgpt_counted or platform != "chat_gpt":
            mention_sets.append(mentions or [])
    counted = [row for row in platforms if chatgpt_counted or row["platform"] != "chat_gpt"]
    cross = {
        platform: calls.get((platform, "cross"))
        for platform in LLM_PLATFORMS
        if chatgpt_counted or platform != "chat_gpt"
    }
    overview = {
        "chatgpt_counted": chatgpt_counted,
        "total_mentions": _sum([row["mentions"] for row in counted]),
        "total_ai_search_volume": _sum([row["ai_search_volume"] for row in counted]),
        "platforms": platforms,
        "monthly_volume": _monthly(mention_sets),
        "share_of_voice": share_of_voice(cross, target, competitors),
    }
    # Cited pages first, then prompts; each by volume, per platform then merged.
    pages = sorted(
        (row for row in rows if row["section"] == "page"),
        key=lambda row: (-_by_volume(row, "captured_volume"), -(row["mentions"] or 0)),
    )
    queries = sorted(
        (row for row in rows if row["section"] == "query"),
        key=lambda row: -_by_volume(row, "ai_search_volume"),
    )
    return overview, pages + queries
