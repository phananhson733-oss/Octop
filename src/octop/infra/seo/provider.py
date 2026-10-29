"""Direct DataForSEO transport. One POST per call; never retry a charged request.

Labs mapping follows OpenSEO d7ea785 research-data.ts/helpers.ts, SERP mapping
follows lib/dataforseo/serp.ts and mcp/tools/get-serp-results.ts, competitor research
follows lib/dataforseo/labs.ts, features/domain/services and mcp/tools/
dataforseo-research-tools.ts (top pages follow OpenSEO 2d8236a domainPagesPage.ts,
backlinks its lib/dataforseo/backlinks.ts and features/backlinks/services), AI answers follow lib/dataforseo/ai-tracking.ts and
features/ai-visibility/providers/dataforseoEvidence.ts, and prompt answers follow 2d8236a
lib/dataforseo/llm-models.ts, lib/dataforseo/ai.ts and features/ai-search/services/
promptExplorer.ts, brand mentions its lib/dataforseo/ai.ts and shared.ts (MIT).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from itertools import zip_longest
from typing import Any
from urllib.parse import unquote_plus, urlsplit, urlunsplit

import httpx


class ProviderFailure(Exception):
    def __init__(
        self,
        code: str,
        *,
        cost: float | None = None,
        unknown: bool = False,
        receipt: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.cost = cost
        self.unknown = unknown
        self.receipt = receipt or {}


def normalize_keyword(value: str) -> str:
    return value.strip().lower()


def normalize_intent(raw: str | None) -> str:
    for needle, intent in [
        ("inform", "informational"),
        ("commerc", "commercial"),
        ("transact", "transactional"),
        ("navig", "navigational"),
    ]:
        if needle in (raw or "").lower():
            return intent
    return "unknown"


def interleave(
    first: list[dict[str, Any]], second: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pair in zip_longest(first, second):
        for row in pair:
            if row is not None and row["keyword"] not in seen and len(rows) < limit:
                seen.add(row["keyword"])
                rows.append(row)
    return rows


def map_labs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        keyword = normalize_keyword(item.get("keyword") or "")
        if not keyword or keyword in seen:
            continue
        seen.add(keyword)
        original = item.get("keyword_info") or {}
        clickstream = item.get("keyword_info_normalized_with_clickstream") or {}
        info = clickstream if clickstream.get("search_volume") is not None else original
        volume_source = (
            "clickstream"
            if info is clickstream
            else "keyword_info"
            if original.get("search_volume") is not None
            else "unknown"
        )
        rows.append(
            {
                "keyword": keyword,
                "search_volume": info.get("search_volume"),
                "volume_source": volume_source,
                "cpc": original.get("cpc"),
                "competition": original.get("competition"),
                "keyword_difficulty": (item.get("keyword_properties") or {}).get(
                    "keyword_difficulty"
                ),
                "intent": normalize_intent(
                    (item.get("search_intent_info") or {}).get("main_intent")
                ),
                "trend": [
                    {
                        "year": v.get("year") or 0,
                        "month": v.get("month") or 0,
                        "search_volume": v.get("search_volume"),
                    }
                    for v in info.get("monthly_searches") or []
                ],
            }
        )
    return rows


def _cost(value: Any) -> float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    ):
        return float(value)
    return None


def map_serp(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep every SERP element: ``rank`` counts all of them, ``organic_rank`` organic only."""
    rows: list[dict[str, Any]] = []
    for item in items:
        organic = item.get("type") == "organic"
        backlinks = item.get("backlinks_info") or {}
        rows.append(
            {
                "type": item.get("type") or "unknown",
                "rank": (
                    item["rank_absolute"]
                    if item.get("rank_absolute") is not None
                    else item.get("rank_group")
                ),
                # rank_group is the position among organic results, what rank trackers show.
                "organic_rank": item.get("rank_group") if organic else None,
                "title": item.get("title"),
                "url": item.get("url"),
                "domain": item.get("domain"),
                "description": item.get("description"),
                "etv": item.get("etv"),
                "estimated_paid_traffic_cost": item.get("estimated_paid_traffic_cost"),
                "referring_domains": backlinks.get("referring_domains"),
                "backlinks": backlinks.get("backlinks"),
            }
        )
    return rows


def map_rank_check(items: list[dict[str, Any]], target: str) -> dict[str, Any]:
    """The target's first organic listing (subdomains count) and the SERP's feature types.

    ``position`` is ``rank_group``, the place among organic results, which is what people
    mean by "my ranking"; ``rank_absolute`` would also count ads, maps and answer boxes.
    """
    target = target.lower()
    match = next(
        (
            item
            for item in items
            if item.get("type") == "organic"
            and isinstance(item.get("domain"), str)
            and (item["domain"].lower() == target or item["domain"].lower().endswith("." + target))
        ),
        None,
    )
    return {
        "position": (
            None
            if match is None
            else match["rank_group"]
            if match.get("rank_group") is not None
            else match.get("rank_absolute")
        ),
        "url": match.get("url") if match else None,
        "serp_features": sorted({str(item["type"]) for item in items if item.get("type")}),
    }


class _MalformedResult(Exception):
    """The task succeeded but its result does not have the documented shape."""


def _item_list(result: dict[str, Any]) -> list[dict[str, Any]]:
    items = result.get("items") or []
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise _MalformedResult
    return items


def map_domain_overview(result: dict[str, Any]) -> dict[str, Any]:
    """Organic footprint from domain_rank_overview; backlinks are not bought here."""
    items = _item_list(result)
    organic = ((items[0] if items else {}).get("metrics") or {}).get("organic") or {}
    return {
        "organic_traffic": organic.get("etv"),
        "organic_keywords": organic.get("count"),
        "pos_1": organic.get("pos_1"),
        "pos_2_3": organic.get("pos_2_3"),
        "pos_4_10": organic.get("pos_4_10"),
    }


def map_ranked_keywords(result: dict[str, Any]) -> dict[str, Any]:
    """ranked_keywords rows (domainKeywordMapper.ts) and the domain's total keyword count."""
    rows: list[dict[str, Any]] = []
    for item in _item_list(result):
        data = item.get("keyword_data") or {}
        keyword = normalize_keyword(data.get("keyword") or item.get("keyword") or "")
        if not keyword:
            continue
        info = data.get("keyword_info") or {}
        element = item.get("ranked_serp_element") or {}
        serp_item = element.get("serp_item") or {}
        rows.append(
            {
                "keyword": keyword,
                "rank": serp_item.get("rank_absolute"),
                "organic_rank": serp_item.get("rank_group"),
                "url": serp_item.get("url"),
                "etv": serp_item.get("etv"),
                "search_volume": info.get("search_volume"),
                "cpc": info.get("cpc"),
                "keyword_difficulty": (data.get("keyword_properties") or {}).get(
                    "keyword_difficulty"
                ),
                "intent": normalize_intent(
                    (data.get("search_intent_info") or {}).get("main_intent")
                ),
            }
        )
    return {"total_count": result.get("total_count"), "rows": rows}


def map_relevant_pages(result: dict[str, Any]) -> dict[str, Any]:
    """relevant_pages rows (domainPagesPage.ts mapPageItem) and the domain's page count."""
    rows: list[dict[str, Any]] = []
    for item in _item_list(result):
        url = item.get("page_address")
        if not isinstance(url, str) or not url:
            continue
        organic = (item.get("metrics") or {}).get("organic") or {}
        rows.append(
            {
                "url": url,
                "etv": organic.get("etv"),
                "keywords": organic.get("count"),
                "pos_1": organic.get("pos_1"),
                "pos_2_3": organic.get("pos_2_3"),
                "pos_4_10": organic.get("pos_4_10"),
            }
        )
    return {"total_count": result.get("total_count"), "rows": rows}


# OpenSEO's "best links only" view (DEFAULT_BACKLINKS_SPAM_THRESHOLD in schemas/backlinks.ts).
BACKLINKS_SPAM_THRESHOLD = 40


def _either(item: dict[str, Any], *keys: str) -> Any:
    """The first key present: DataForSEO sends both ``*_reffering_*`` and the fixed spelling."""
    return next((item[key] for key in keys if item.get(key) is not None), None)


def map_backlinks_summary(result: dict[str, Any]) -> dict[str, Any]:
    """backlinks/summary totals (backlinksServiceData.ts buildOverviewResult)."""
    return {
        "rank": result.get("rank"),
        "backlinks": result.get("backlinks"),
        "referring_pages": result.get("referring_pages"),
        "referring_domains": result.get("referring_domains"),
        "broken_backlinks": result.get("broken_backlinks"),
        "broken_pages": result.get("broken_pages"),
        "backlinks_spam_score": result.get("backlinks_spam_score"),
        "target_spam_score": (result.get("info") or {}).get("target_spam_score"),
        "new_backlinks": result.get("new_backlinks"),
        "lost_backlinks": result.get("lost_backlinks"),
        "new_referring_domains": _either(result, "new_referring_domains", "new_reffering_domains"),
        "lost_referring_domains": _either(
            result, "lost_referring_domains", "lost_reffering_domains"
        ),
    }


def map_backlinks_history(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Monthly backlink totals, oldest first; rows without a date are dropped."""
    rows = [
        {
            "section": "history",
            "date": item["date"][:10],
            "backlinks": item.get("backlinks"),
            "referring_domains": item.get("referring_domains"),
            "rank": item.get("rank"),
            "new_backlinks": item.get("new_backlinks"),
            "lost_backlinks": item.get("lost_backlinks"),
            "new_referring_domains": _either(
                item, "new_referring_domains", "new_reffering_domains"
            ),
            "lost_referring_domains": _either(
                item, "lost_referring_domains", "lost_reffering_domains"
            ),
        }
        for item in _item_list(result)
        if isinstance(item.get("date"), str) and item["date"]
    ]
    return sorted(rows, key=lambda row: row["date"])


def _counted(rows: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, Any]:
    return {"total_count": result.get("total_count"), "rows": rows}


def map_backlinks(result: dict[str, Any]) -> dict[str, Any]:
    """backlinks/backlinks rows (backlinksRowMappers.ts mapBacklinksRows)."""
    rows = [
        {
            "section": "backlink",
            "domain_from": item.get("domain_from"),
            "url_from": item.get("url_from"),
            "url_to": item.get("url_to"),
            "anchor": item.get("anchor"),
            "item_type": item.get("item_type"),
            "dofollow": item.get("dofollow"),
            "rank": item.get("rank"),
            "domain_from_rank": item.get("domain_from_rank"),
            "spam_score": _either(item, "backlink_spam_score", "backlinks_spam_score"),
            "first_seen": item.get("first_seen"),
            "last_seen": _either(item, "lost_date", "last_seen", "last_visited"),
            "is_lost": item["is_lost"]
            if item.get("is_lost") is not None
            else bool(item.get("lost_date")),
            "is_broken": bool(item.get("is_broken")),
        }
        for item in _item_list(result)
    ]
    return _counted(rows, result)


def map_referring_domains(result: dict[str, Any]) -> dict[str, Any]:
    """backlinks/referring_domains rows (mapReferringDomainsRows)."""
    rows = [
        {
            "section": "referring_domain",
            "domain": item.get("domain"),
            "backlinks": item.get("backlinks"),
            "referring_pages": item.get("referring_pages"),
            "rank": item.get("rank"),
            "spam_score": item.get("backlinks_spam_score"),
            "first_seen": item.get("first_seen"),
            "broken_backlinks": item.get("broken_backlinks"),
        }
        for item in _item_list(result)
    ]
    return _counted(rows, result)


def map_linked_pages(result: dict[str, Any]) -> dict[str, Any]:
    """backlinks/domain_pages_summary rows (mapTopPagesRows): the most linked-to pages."""
    rows = [
        {
            "section": "page",
            "url": _either(item, "page", "url"),
            "backlinks": item.get("backlinks"),
            "referring_domains": item.get("referring_domains"),
            "rank": item.get("rank"),
            "broken_backlinks": item.get("broken_backlinks"),
        }
        for item in _item_list(result)
    ]
    return _counted(rows, result)


def map_serp_competitors(result: dict[str, Any]) -> list[dict[str, Any]]:
    """serp_competitors rows, most visible first like find_serp_competitors' default sort."""
    rows = [
        {
            "domain": item["domain"],
            "visibility": item.get("visibility"),
            "avg_position": item.get("avg_position"),
            "median_position": item.get("median_position"),
            "etv": item.get("etv"),
            "keywords_count": item.get("keywords_count"),
            "keywords_positions": item.get("keywords_positions") or {},
        }
        for item in _item_list(result)
        if isinstance(item.get("domain"), str) and item["domain"]
    ]
    return sorted(rows, key=lambda row: -(row["visibility"] or 0))


AI_ENGINES = ("chatgpt", "gemini", "google_ai_overview")
_AI_PATHS = {
    "chatgpt": "ai_optimization/chat_gpt/llm_scraper/live/advanced",
    "gemini": "ai_optimization/gemini/llm_scraper/live/advanced",
    "google_ai_overview": "serp/google/organic/live/advanced",
}
# Live answers can take up to 120 seconds.
AI_TIMEOUT_SECONDS = 150
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)\s]*\)")
_LINK = re.compile(r"\[((?:[^\[\]]|\[[^\]]*\])*)\]\((https?://[^)\s]+)\)")
_MARKS = re.compile(r"\*\*|__|`")
_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_BULLET = re.compile(r"^(\s*)[*+-]\s+", re.MULTILINE)
_TRACKING_PARAM = re.compile(r"utm_.*|gclid|fbclid|msclkid", re.IGNORECASE)


def safe_http_url(value: Any) -> str | None:
    """``value`` when it is an http(s) URL without credentials, else ``None``.

    Answers come from LLMs, which a crafted prompt can coax into ``javascript:`` links, and
    a ``user@host`` link can show one host and open another.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parts = urlsplit(value)
        if (
            parts.scheme.lower() not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
        ):
            return None
        _ = parts.port  # raises on a malformed port
    except ValueError:
        return None
    return value


def _citation_pill(label: str, url: str) -> bool:
    """``[1]`` or ``[example.com]`` marks a source, not prose naming a brand."""
    text = re.sub(r"^\[|\]$", "", label).strip().lower()
    if re.fullmatch(r"[0-9]+", text):
        return True
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    return text.removeprefix("www.") == host.removeprefix("www.")


def markdown_to_text(markdown: str) -> str:
    """Answer prose for brand matching: no images, citation pills or markdown marks."""
    text = _IMAGE.sub("", markdown)
    text = _LINK.sub(lambda m: "" if _citation_pill(m.group(1), m.group(2)) else m.group(1), text)
    text = _MARKS.sub("", text)
    text = _HEADING.sub("", text)
    return _BULLET.sub(r"\1", text)


def _citation_url(raw: str) -> tuple[str, str] | None:
    """The URL without its fragment and tracking parameters, and its host."""
    if safe_http_url(raw) is None:
        return None
    parts = urlsplit(raw)
    query = "&".join(
        pair
        for pair in parts.query.split("&")
        if pair and not _TRACKING_PARAM.fullmatch(unquote_plus(pair.split("=", 1)[0]))
    )
    url = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", query, ""))
    return url, parts.hostname or ""


def _citations(sources: list[Any]) -> list[dict[str, Any]]:
    """Safe cited pages, first citation first, one per URL."""
    found: dict[str, dict[str, Any]] = {}
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("url"), str):
            continue
        cited = _citation_url(source["url"]) if len(source["url"]) <= 16_384 else None
        if cited is None:
            continue
        url, domain = cited
        title = source.get("title")
        title = title if isinstance(title, str) and len(title) <= 10_000 else None
        if url in found:
            found[url]["title"] = found[url]["title"] or title
            continue
        found[url] = {"url": url, "domain": domain, "title": title, "position": len(found) + 1}
    return list(found.values())


def _sources(value: Any) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 2_000:
        raise _MalformedResult
    return value


def map_ai_answer(result: dict[str, Any], engine: str) -> dict[str, Any]:
    """An engine's answer and the pages it cites; no answer when the engine showed none.

    The LLM scrapers return the answer as ``markdown`` with ``sources``. Google shows an AI
    Overview as one SERP item, with references on it and on each of its parts.
    """
    if engine == "google_ai_overview":
        overview = next(
            (item for item in _item_list(result) if item.get("type") == "ai_overview"), {}
        )
        parts = overview.get("items") or []
        if not isinstance(parts, list) or any(not isinstance(part, dict) for part in parts):
            raise _MalformedResult
        markdown = overview.get("markdown")
        sources = _sources(overview.get("references"))
        for part in parts:
            sources = sources + _sources(part.get("references"))
    else:
        markdown = result.get("markdown")
        sources = _sources(result.get("sources"))
    if markdown is not None and (not isinstance(markdown, str) or len(markdown) > 1_000_000):
        raise _MalformedResult
    text = markdown_to_text(markdown) if markdown else ""
    answered = bool(text.strip())
    return {
        "answer_markdown": markdown if answered else None,
        "answer_text": text if answered else "",
        "citations": _citations(sources),
    }


# The four models OpenSEO's Prompt Explorer asks, in its order.
LLM_MODELS = ("chat_gpt", "claude", "gemini", "perplexity")
# llm-models.ts: the snapshot used when the free catalog is unreachable or names no
# flagship, the pins that win while the catalog lists them, and the flagship rule.
LLM_FALLBACK_NAMES = {
    "chat_gpt": ("gpt-5.6-luna", "gpt-5.5", "gpt-5.4", "gpt-5.2", "gpt-5.1", "gpt-5"),
    "claude": ("claude-sonnet-5", "claude-sonnet-4-6", "claude-sonnet-4-5"),
    "gemini": ("gemini-2.5-pro",),
    "perplexity": ("sonar-reasoning-pro", "sonar-pro", "sonar"),
}
LLM_PINNED_NAMES = {"chat_gpt": "gpt-5.6-luna", "claude": "claude-sonnet-5"}
_LLM_LATEST_RULES = {
    "chat_gpt": re.compile(r"gpt-(\d+(?:\.\d+)*)"),
    "claude": re.compile(r"claude-sonnet-(\d+(?:-\d+)*)"),
    "gemini": re.compile(r"gemini-(\d+(?:\.\d+)*)-pro"),
    "perplexity": re.compile(r"sonar-reasoning-pro()"),
}
# prompt-search-countries.ts: Claude documents this list, Gemini rejects the field,
# ChatGPT and Perplexity take any ISO 3166-1 alpha-2 code.
_CLAUDE_COUNTRIES = (
    "AR AT AU BE BR CA CH CL CN DE DK ES FI FR GB HK ID IN IT JP KR MX MY NL NO NZ PH PL PT "
    "RU SA SE TR TW US ZA"
)
CLAUDE_SEARCH_COUNTRIES = frozenset(_CLAUDE_COUNTRIES.split())
# promptExplorer.ts: reasoning models spend hidden tokens from this budget.
LLM_MAX_OUTPUT_TOKENS = 4096


def supports_search_country(model: str, country: str) -> bool:
    if model == "gemini":
        return False
    return model != "claude" or country in CLAUDE_SEARCH_COUNTRIES


def pick_llm_model_name(model: str, names: list[str]) -> str:
    """The pinned name while listed, else the highest flagship alias, else a listed fallback."""
    pinned = LLM_PINNED_NAMES.get(model)
    if pinned is not None and pinned in names:
        return pinned
    best: tuple[list[int], str] | None = None
    for name in names:
        match = _LLM_LATEST_RULES[model].fullmatch(name)
        if match is None:
            continue
        version = [int(part) for part in re.split(r"[.-]", match.group(1) or "0")]
        # Shorter versions pad with zeros, as compareVersions does.
        if best is None or _pad(version, best[0]) > _pad(best[0], version):
            best = (version, name)
    if best is not None:
        return best[1]
    fallback = LLM_FALLBACK_NAMES[model]
    return next((name for name in fallback if name in names), fallback[0])


def _pad(version: list[int], other: list[int]) -> list[int]:
    return version + [0] * (len(other) - len(version))


def map_llm_response(result: dict[str, Any]) -> dict[str, Any]:
    """The answer's text, the pages it cites, and the searches the model ran."""
    texts: list[str] = []
    citations: dict[str, dict[str, Any]] = {}
    for item in _item_list(result):
        if item.get("type") != "message":
            continue
        sections = item.get("sections") or []
        if not isinstance(sections, list) or any(not isinstance(sec, dict) for sec in sections):
            raise _MalformedResult
        for section in sections:
            text = section.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
            for note in _sources(section.get("annotations")):
                # Annotations are untyped {title, url}; LLM output, so http(s) only.
                url = safe_http_url(note.get("url")) if isinstance(note, dict) else None
                if url is None or url in citations or len(url) > 2048:
                    continue
                title = note.get("title")
                citations[url] = {
                    "url": url,
                    "domain": urlsplit(url).hostname,
                    "title": title[:300] if isinstance(title, str) else None,
                }
    queries = result.get("fan_out_queries") or []
    if not isinstance(queries, list):
        raise _MalformedResult
    tokens = _cost(result.get("output_tokens"))
    name = result.get("model_name")
    return {
        "model_name": name if isinstance(name, str) else None,
        "text": "\n\n".join(texts).strip(),
        "citations": list(citations.values())[:25],
        "fan_out_queries": [q[:500] for q in queries if isinstance(q, str)][:20],
        "output_tokens": round(tokens) if tokens is not None else None,
        "web_search": result.get("web_search") is True,
    }


# Platforms LLM Mentions covers (shared.ts LlmPlatform); ChatGPT's data is US English only.
LLM_PLATFORMS = ("chat_gpt", "google")
CHATGPT_MARKET = (2840, "en")


def llm_target(kind: str, value: str) -> dict[str, Any]:
    """shared.ts buildLlmTarget: a domain with its subdomains, or a word-matched brand."""
    if kind == "domain":
        return {
            "domain": value,
            "include_subdomains": True,
            "search_filter": "include",
            "search_scope": ["any"],
        }
    return {
        "keyword": value,
        "search_filter": "include",
        "search_scope": ["any", "brand_entities"],
        "match_type": "word_match",
    }


def map_llm_aggregated(result: dict[str, Any]) -> list[dict[str, Any]]:
    """aggregated_metrics' per-platform totals."""
    total = result.get("total") or {}
    groups = total.get("platform") if isinstance(total, dict) else None
    if groups is not None and not isinstance(groups, list):
        raise _MalformedResult
    return [group for group in groups or [] if isinstance(group, dict)]


class DataForSeoClient:
    def __init__(
        self, login: str, password: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._auth = httpx.BasicAuth(login, password)
        self._transport = transport

    async def fetch(
        self,
        *,
        source: str,
        seed: str,
        location_code: int,
        language_code: str,
        limit: int,
        clickstream: bool,
        group_keywords: bool,
    ) -> tuple[list[dict[str, Any]], float | None, dict[str, Any]]:
        endpoints = {
            "suggestions": "keyword_suggestions",
            "ideas": "keyword_ideas",
            "related": "related_keywords",
        }
        payload: dict[str, Any] = {
            "location_code": location_code,
            "language_code": language_code,
            "limit": limit,
            "include_clickstream_data": clickstream,
            "ignore_synonyms": not group_keywords,
            "include_serp_info": False,
        }
        if source == "ideas":
            payload.update(keywords=[seed], closely_variants=False)
        else:
            payload["keyword"] = seed
        if source == "suggestions":
            payload.update(include_seed_keyword=True, exact_match=False)
        if source == "related":
            payload["depth"] = 3

        def rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            if source == "related":
                items = [
                    item["keyword_data"]
                    for item in items
                    if isinstance(item.get("keyword_data"), dict)
                ]
            return map_labs(items)

        return await self._items(
            "dataforseo_labs/google/" + endpoints[source] + "/live", payload, rows
        )

    async def serp(
        self, *, keyword: str, location_code: int, language_code: str, device: str, depth: int
    ) -> tuple[list[dict[str, Any]], float | None, dict[str, Any]]:
        payload = {
            "keyword": keyword,
            "location_code": location_code,
            "language_code": language_code,
            "device": device,
            "os": "windows" if device == "desktop" else "android",
            # DataForSEO bills SERPs in pages of 10 and rejects depth outside 10-100.
            "depth": min(100, max(10, depth)),
        }
        # A valid empty SERP comes back as a charged "No Search Results" task error.
        return await self._items(
            "serp/google/organic/live/advanced", payload, map_serp, no_results_ok=True
        )

    async def rank_check(
        self,
        *,
        keyword: str,
        target: str,
        location_code: int,
        language_code: str,
        device: str,
        depth: int,
    ) -> tuple[dict[str, Any], float | None, dict[str, Any]]:
        """Where ``target`` ranks for ``keyword``, crawling at most ``depth`` results."""
        payload = {
            "keyword": keyword,
            "location_code": location_code,
            "language_code": language_code,
            "device": device,
            "os": "windows" if device == "desktop" else "android",
            "depth": min(100, max(10, depth)),
            # Stop at the page where the target's organic listing shows: only crawled
            # pages are billed. Matching organic results only, as map_rank_check does, so
            # a sitelink or answer-box mention cannot stop the crawl early.
            "stop_crawl_on_match": [{"match_value": target, "match_type": "with_subdomains"}],
            "find_targets_in": ["organic"],
        }
        return await self._result(
            "serp/google/organic/live/advanced",
            payload,
            lambda result: map_rank_check(_item_list(result), target),
            no_results_ok=True,
        )

    async def ai_answer(
        self, *, engine: str, prompt: str, location_code: int, language_code: str
    ) -> tuple[dict[str, Any], float | None, dict[str, Any]]:
        """One live answer from ``engine`` to ``prompt`` in the market."""
        payload: dict[str, Any] = {
            # DataForSEO decodes %-escapes in keywords; escape literal % and +.
            "keyword": prompt.replace("%", "%25").replace("+", "%2B"),
            "location_code": location_code,
            "language_code": language_code,
        }
        if engine == "google_ai_overview":
            # Loads an overview Google renders after the page; refunded when none loads.
            payload.update(depth=10, load_async_ai_overview=True)
        return await self._result(
            _AI_PATHS[engine],
            payload,
            lambda result: map_ai_answer(result, engine),
            no_results_ok=True,
            timeout=AI_TIMEOUT_SECONDS,
        )

    async def llm_model_names(self, model: str) -> list[str]:
        """DataForSEO's free model catalog for ``model``; the snapshot when it cannot be read."""
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=30, follow_redirects=False
            ) as client:
                response = await client.get(
                    f"https://api.dataforseo.com/v3/ai_optimization/{model}/llm_responses/models",
                    auth=self._auth,
                )
            data = response.json()
            task = data["tasks"][0]
            if data.get("status_code") != 20000 or task.get("status_code") != 20000:
                raise ValueError("catalog refused")
            names = [
                row["model_name"]
                for row in task.get("result") or []
                if isinstance(row, dict) and isinstance(row.get("model_name"), str)
            ]
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError):
            names = []
        return names or list(LLM_FALLBACK_NAMES[model])

    async def llm_response(
        self, *, model: str, model_name: str, prompt: str, web_search: bool, country: str | None
    ) -> tuple[dict[str, Any], float | None, dict[str, Any]]:
        """One live answer from ``model_name``; ``country`` only geolocates its web search."""
        payload: dict[str, Any] = {
            "user_prompt": prompt,
            "model_name": model_name,
            "web_search": web_search,
            "max_output_tokens": LLM_MAX_OUTPUT_TOKENS,
        }
        if web_search and model == "claude":
            # Only Claude takes force_web_search; ChatGPT rejects it.
            payload["force_web_search"] = True
        if web_search and country:
            payload["web_search_country_iso_code"] = country
        return await self._result(
            f"ai_optimization/{model}/llm_responses/live",
            payload,
            map_llm_response,
            timeout=AI_TIMEOUT_SECONDS,
        )

    async def _llm_mentions(
        self,
        endpoint: str,
        payload: dict[str, Any],
        mapper: Callable[[dict[str, Any]], Any] = _item_list,
    ) -> tuple[Any, float | None, dict[str, Any]]:
        return await self._result(f"ai_optimization/llm_mentions/{endpoint}/live", payload, mapper)

    async def llm_aggregated_metrics(
        self, *, target: dict[str, Any], platform: str, location_code: int, language_code: str
    ) -> tuple[list[dict[str, Any]], float | None, dict[str, Any]]:
        """How often AI answers on ``platform`` mention ``target``, and their search volume."""
        payload = {
            "target": [target],
            "platform": platform,
            "location_code": location_code,
            "language_code": language_code,
            "internal_list_limit": 20,
        }
        return await self._llm_mentions("aggregated_metrics", payload, map_llm_aggregated)

    async def llm_top_pages(
        self, *, target: dict[str, Any], platform: str, location_code: int, language_code: str
    ) -> tuple[list[dict[str, Any]], float | None, dict[str, Any]]:
        """The pages those answers cite most."""
        payload = {
            "target": [target],
            "platform": platform,
            "location_code": location_code,
            "language_code": language_code,
            "links_scope": "sources",
            "items_list_limit": 10,
            "internal_list_limit": 5,
        }
        return await self._llm_mentions("top_pages", payload)

    async def llm_mentions_search(
        self,
        *,
        target: dict[str, Any],
        platform: str,
        location_code: int,
        language_code: str,
        limit: int,
    ) -> tuple[list[dict[str, Any]], float | None, dict[str, Any]]:
        """A sample of the prompts whose answers mention ``target``."""
        payload = {
            "target": [target],
            "platform": platform,
            "location_code": location_code,
            "language_code": language_code,
            "limit": limit,
        }
        return await self._llm_mentions("search", payload)

    async def llm_cross_aggregated(
        self,
        *,
        groups: list[tuple[str, dict[str, Any]]],
        platform: str,
        location_code: int,
        language_code: str,
    ) -> tuple[list[dict[str, Any]], float | None, dict[str, Any]]:
        """Mentions of each ``(aggregation_key, target)`` group, compared in one call."""
        payload = {
            "targets": [{"aggregation_key": key, "target": [target]} for key, target in groups],
            "platform": platform,
            "location_code": location_code,
            "language_code": language_code,
            "internal_list_limit": 5,
        }
        return await self._llm_mentions("cross_aggregated_metrics", payload)

    async def domain_overview(
        self, *, target: str, location_code: int, language_code: str
    ) -> tuple[dict[str, Any], float | None, dict[str, Any]]:
        payload = {
            "target": target,
            "location_code": location_code,
            "language_code": language_code,
            "limit": 1,
        }
        return await self._result(
            "dataforseo_labs/google/domain_rank_overview/live", payload, map_domain_overview
        )

    async def ranked_keywords(
        self, *, target: str, location_code: int, language_code: str, limit: int
    ) -> tuple[dict[str, Any], float | None, dict[str, Any]]:
        # A domain target covers the host and its subdomains; organic rankings only.
        payload = {
            "target": target,
            "location_code": location_code,
            "language_code": language_code,
            "limit": limit,
            "order_by": ["ranked_serp_element.serp_item.etv,desc"],
            "item_types": ["organic"],
        }
        return await self._result(
            "dataforseo_labs/google/ranked_keywords/live", payload, map_ranked_keywords
        )

    async def relevant_pages(
        self, *, target: str, location_code: int, language_code: str, limit: int
    ) -> tuple[dict[str, Any], float | None, dict[str, Any]]:
        """The domain's pages that draw the most estimated organic traffic."""
        payload = {
            "target": target,
            "location_code": location_code,
            "language_code": language_code,
            "limit": limit,
            "order_by": ["metrics.organic.etv,desc"],
        }
        return await self._result(
            "dataforseo_labs/google/relevant_pages/live", payload, map_relevant_pages
        )

    @staticmethod
    def _backlinks_payload(target: str) -> dict[str, Any]:
        # buildCommonPayload in backlinks.ts: the domain with its subdomains, live links only.
        return {
            "target": target,
            "include_subdomains": True,
            "include_indirect_links": True,
            "exclude_internal_backlinks": True,
            "backlinks_status_type": "live",
            "rank_scale": "one_hundred",
        }

    async def backlinks_summary(
        self, *, target: str
    ) -> tuple[dict[str, Any], float | None, dict[str, Any]]:
        return await self._result(
            "backlinks/summary/live", self._backlinks_payload(target), map_backlinks_summary
        )

    async def backlinks_history(
        self, *, target: str, date_from: str, date_to: str
    ) -> tuple[list[dict[str, Any]], float | None, dict[str, Any]]:
        # history/live takes a bare hostname and no subdomain switch.
        payload = {
            "target": target,
            "date_from": date_from,
            "date_to": date_to,
            "rank_scale": "one_hundred",
        }
        return await self._result("backlinks/history/live", payload, map_backlinks_history)

    async def backlinks(
        self, *, target: str, limit: int
    ) -> tuple[dict[str, Any], float | None, dict[str, Any]]:
        """The strongest live backlinks, one per linking domain, likely spam left out."""
        payload = self._backlinks_payload(target) | {
            "limit": limit,
            "order_by": ["rank,desc"],
            "mode": "one_per_domain",
            "filters": [
                ["backlink_spam_score", "<", BACKLINKS_SPAM_THRESHOLD],
                "or",
                ["backlink_spam_score", "=", None],
            ],
        }
        return await self._result("backlinks/backlinks/live", payload, map_backlinks)

    async def referring_domains(
        self, *, target: str, limit: int
    ) -> tuple[dict[str, Any], float | None, dict[str, Any]]:
        payload = self._backlinks_payload(target) | {
            "limit": limit,
            "order_by": ["backlinks,desc"],
            "filters": ["backlinks_spam_score", "<=", BACKLINKS_SPAM_THRESHOLD],
        }
        return await self._result(
            "backlinks/referring_domains/live", payload, map_referring_domains
        )

    async def linked_pages(
        self, *, target: str, limit: int
    ) -> tuple[dict[str, Any], float | None, dict[str, Any]]:
        payload = self._backlinks_payload(target) | {
            "limit": limit,
            "order_by": ["backlinks,desc"],
        }
        return await self._result("backlinks/domain_pages_summary/live", payload, map_linked_pages)

    async def serp_competitors(
        self, *, keywords: list[str], location_code: int, language_code: str, limit: int
    ) -> tuple[list[dict[str, Any]], float | None, dict[str, Any]]:
        payload = {
            "keywords": keywords,
            "location_code": location_code,
            "language_code": language_code,
            "item_types": ["organic"],
            "limit": limit,
        }
        return await self._result(
            "dataforseo_labs/google/serp_competitors/live", payload, map_serp_competitors
        )

    async def _items(
        self,
        path: str,
        payload: dict[str, Any],
        mapper: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
        *,
        no_results_ok: bool = False,
    ) -> tuple[list[dict[str, Any]], float | None, dict[str, Any]]:
        return await self._result(
            path, payload, lambda result: mapper(_item_list(result)), no_results_ok=no_results_ok
        )

    async def _result[T](
        self,
        path: str,
        payload: dict[str, Any],
        mapper: Callable[[dict[str, Any]], T],
        *,
        no_results_ok: bool = False,
        timeout: float = 60,
    ) -> tuple[T, float | None, dict[str, Any]]:
        """One charged task; ``mapper`` reads its first result (``{}`` when there is none)."""
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=timeout, follow_redirects=False
            ) as client:
                response = await client.post(
                    "https://api.dataforseo.com/v3/" + path, json=[payload], auth=self._auth
                )
        except httpx.HTTPError as exc:
            raise ProviderFailure("transport_unknown", unknown=True) from exc
        cost: float | None = None
        receipt: dict[str, Any] = {"http_status": response.status_code}
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("invalid envelope")
            tasks = data.get("tasks")
            task = (
                tasks[0]
                if isinstance(tasks, list) and len(tasks) == 1 and isinstance(tasks[0], dict)
                else None
            )
            receipt.update(
                status_code=data.get("status_code"),
                task_id=task.get("id") if task else None,
                task_status=task.get("status_code") if task else None,
            )
            cost = _cost(task.get("cost")) if task else _cost(data.get("cost"))
            if (
                no_results_ok
                and response.is_success
                and data.get("status_code") == 20000
                and task is not None
                and "no search results" in str(task.get("status_message") or "").lower()
            ):
                return mapper({}), cost, receipt
            if (
                not response.is_success
                or data.get("status_code") != 20000
                or task is None
                or task.get("status_code") != 20000
            ):
                raise ProviderFailure("provider_rejected", cost=cost, receipt=receipt)
            result = task.get("result")
            if result is None or result == []:
                return mapper({}), cost, receipt
            if not isinstance(result, list) or not isinstance(result[0], dict):
                raise ProviderFailure("malformed_result", cost=cost, receipt=receipt)
            return mapper(result[0]), cost, receipt
        except _MalformedResult as exc:
            raise ProviderFailure("malformed_result", cost=cost, receipt=receipt) from exc
        except (ValueError, TypeError, AttributeError) as exc:
            raise ProviderFailure(
                "malformed_response", cost=cost, unknown=cost is None, receipt=receipt
            ) from exc
