"""Direct DataForSEO transport. One POST per call; never retry a charged request.

Labs mapping follows OpenSEO d7ea785 research-data.ts/helpers.ts, SERP mapping
follows lib/dataforseo/serp.ts and mcp/tools/get-serp-results.ts, competitor research
follows lib/dataforseo/labs.ts, features/domain/services and mcp/tools/
dataforseo-research-tools.ts (MIT).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from itertools import zip_longest
from typing import Any

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
        info = clickstream if clickstream.get("search_volume") else original
        rows.append(
            {
                "keyword": keyword,
                "search_volume": info.get("search_volume"),
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
                        "search_volume": v.get("search_volume") or 0,
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
    ) -> tuple[T, float | None, dict[str, Any]]:
        """One charged task; ``mapper`` reads its first result (``{}`` when there is none)."""
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=60, follow_redirects=False
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
