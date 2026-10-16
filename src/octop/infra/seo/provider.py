"""Direct DataForSEO Labs transport. One POST per call; never retry a charged request.

Mapping follows OpenSEO d7ea785 research-data.ts/helpers.ts (MIT).
"""

from __future__ import annotations

import math
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
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=60, follow_redirects=False
            ) as client:
                response = await client.post(
                    "https://api.dataforseo.com/v3/dataforseo_labs/google/"
                    + endpoints[source]
                    + "/live",
                    json=[payload],
                    auth=self._auth,
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
                not response.is_success
                or data.get("status_code") != 20000
                or task is None
                or task.get("status_code") != 20000
            ):
                raise ProviderFailure("provider_rejected", cost=cost, receipt=receipt)
            result = task.get("result")
            if result is None or result == []:
                return [], cost, receipt
            if not isinstance(result, list) or not isinstance(result[0], dict):
                raise ProviderFailure("malformed_result", cost=cost, receipt=receipt)
            items = result[0].get("items") or []
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ProviderFailure("malformed_result", cost=cost, receipt=receipt)
            if source == "related":
                items = [
                    item["keyword_data"]
                    for item in items
                    if isinstance(item.get("keyword_data"), dict)
                ]
            return map_labs(items), cost, receipt
        except (ValueError, TypeError, AttributeError) as exc:
            raise ProviderFailure(
                "malformed_response", cost=cost, unknown=cost is None, receipt=receipt
            ) from exc
