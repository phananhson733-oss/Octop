"""Google Search Console: Search Analytics, URL inspection, and the page's report shaping.

Ported from OpenSEO d7ea785 ``server/lib/gscClient.ts``, ``features/gsc/searchAnalytics.ts``
and ``features/gsc/searchPerformanceReport.ts``. Search Console is free first-party data,
so nothing here is metered or cached.
"""

from __future__ import annotations

import calendar
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote

import httpx

API_BASE = "https://www.googleapis.com/webmasters/v3"
INSPECT_URL = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
DIMENSIONS = ("query", "page", "country", "device", "date", "searchAppearance")
OPERATORS = ("equals", "notEquals", "contains", "notContains")
SEARCH_TYPES = ("web", "image", "video", "news", "googleNews", "discover")
DATE_RANGES = (
    "last_7_days",
    "last_28_days",
    "last_3_months",
    "last_6_months",
    "last_12_months",
    "last_16_months",
)
DEFAULT_ROW_LIMIT = 250
# Rows per call; Google allows 25000, this keeps fetched == returned for the expert.
MAX_ROW_LIMIT = 1000
# Search Console data trails by about 2-3 days; convenience ranges end before that.
_LAG_DAYS = 3
_RANGE_MONTHS = {"last_3_months": 3, "last_6_months": 6, "last_12_months": 12, "last_16_months": 16}
_RANGE_DAYS = {"last_7_days": 7, "last_28_days": 28}
# "Striking distance": already ranking, not yet near the top, where content work most
# plausibly moves real traffic.
STRIKING_MIN_POSITION = 5
STRIKING_MAX_POSITION = 20
_STRIKING_LIMIT = 100
UNVERIFIED_PERMISSION = "siteUnverifiedUser"


def _sub_months(day: date, months: int) -> date:
    """Calendar months back in UTC, clamping the day to the target month's length."""
    index = day.year * 12 + day.month - 1 - months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def resolve_date_range(
    *,
    date_range: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    today: date,
) -> tuple[str, str]:
    """Explicit dates win; otherwise a convenience range ending three days ago.

    Starts are clamped to Search Console's 16-month lower bound.
    """
    floor = _sub_months(today, 16).isoformat()
    if start_date and end_date:
        return max(start_date, floor), end_date
    end = today - timedelta(days=_LAG_DAYS)
    name = date_range or "last_28_days"
    if name in _RANGE_DAYS:
        start = end - timedelta(days=_RANGE_DAYS[name])
    else:
        start = _sub_months(end, _RANGE_MONTHS[name])
    return max(start.isoformat(), floor), end.isoformat()


def build_request(
    *,
    today: date,
    dimensions: list[str] | None = None,
    date_range: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    filters: list[dict[str, str]] | None = None,
    row_limit: int | None = None,
    start_row: int | None = None,
    search_type: str | None = None,
    data_state: str | None = None,
) -> dict[str, Any]:
    """The ``searchAnalytics.query`` body. Flat filters go into ``dimensionFilterGroups``:
    Search Console silently ignores a top-level ``filters`` field."""
    start, end = resolve_date_range(
        date_range=date_range, start_date=start_date, end_date=end_date, today=today
    )
    request: dict[str, Any] = {
        "startDate": start,
        "endDate": end,
        "dimensions": list(dimensions) if dimensions else ["query"],
        "rowLimit": min(
            max(DEFAULT_ROW_LIMIT if row_limit is None else row_limit, 1), MAX_ROW_LIMIT
        ),
        "type": search_type or "web",
        "dataState": data_state or "all",
    }
    if start_row and start_row > 0:
        request["startRow"] = start_row
    if filters:
        request["dimensionFilterGroups"] = [{"groupType": "and", "filters": list(filters)}]
    return request


def sum_totals(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Clicks and impressions summed; position weighted by impressions; zeros when empty."""
    clicks = sum(row["clicks"] for row in rows)
    impressions = sum(row["impressions"] for row in rows)
    weighted = sum(row.get("position", 0) * row["impressions"] for row in rows)
    return {
        "clicks": clicks,
        "impressions": impressions,
        "ctr": clicks / impressions if impressions else 0,
        "position": weighted / impressions if impressions else 0,
    }


def dimension_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Single-dimension rows (query, page, country) as keyed table rows."""
    return [
        {
            "key": row["keys"][0],
            "clicks": row["clicks"],
            "impressions": row["impressions"],
            "ctr": row["ctr"],
            "position": row.get("position"),
        }
        for row in rows
        if row.get("keys") and row["keys"][0]
    ]


def striking_distance(
    rows: list[dict[str, Any]], limit: int = _STRIKING_LIMIT
) -> list[dict[str, Any]]:
    """One row per query from ``["query", "page"]`` rows, kept when its best page ranks 5-20.

    A query fans out over every page it shows on. When any page already ranks above 5
    the site effectively ranks near the top, so each query collapses to its best page
    (lowest position, ties broken by impressions) before the band check.
    """
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        keys = row.get("keys") or []
        if len(keys) < 2 or not keys[0] or not keys[1] or row.get("position") is None:
            continue
        current = best.get(keys[0])
        if (
            current is None
            or row["position"] < current["position"]
            or (
                row["position"] == current["position"]
                and row["impressions"] > current["impressions"]
            )
        ):
            best[keys[0]] = {
                "query": keys[0],
                "page": keys[1],
                "clicks": row["clicks"],
                "impressions": row["impressions"],
                "position": row["position"],
            }
    kept = [
        row
        for row in best.values()
        if STRIKING_MIN_POSITION <= row["position"] <= STRIKING_MAX_POSITION
    ]
    return sorted(kept, key=lambda row: -row["impressions"])[:limit]


def previous_period(start_date: str, end_date: str) -> tuple[str, str]:
    """The same-length window that ends the day before ``start_date``."""
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    length = max(end - start, timedelta())
    previous_end = start - timedelta(days=1)
    return (previous_end - length).isoformat(), previous_end.isoformat()


class GscApiError(Exception):
    """A Search Console call answered with a non-2xx status."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Search Console API error ({status})")
        self.status = status
        self.body = body

    @property
    def api_disabled(self) -> bool:
        """The API is off in the OAuth client's Cloud project: setup, not a dead grant."""
        return self.status == 403 and "SERVICE_DISABLED" in self.body

    @property
    def denied(self) -> bool:
        return self.status in (401, 403) and not self.api_disabled


class GscClient:
    """Search Console calls with an access token from ``token`` (refreshed by the caller)."""

    def __init__(
        self,
        token: Callable[[], Awaitable[str]],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._transport = transport

    async def _request(self, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        async with httpx.AsyncClient(timeout=60, transport=self._transport) as client:
            if body is None:
                response = await client.get(url, headers=headers)
            else:
                response = await client.post(url, headers=headers, json=body)
        if response.status_code // 100 != 2:
            raise GscApiError(response.status_code, response.text[:2000])
        data = response.json()
        return data if isinstance(data, dict) else {}

    async def list_sites(self) -> list[dict[str, Any]]:
        """Properties on the grant: ``siteUrl`` and ``permissionLevel``."""
        data = await self._request(f"{API_BASE}/sites")
        return [entry for entry in data.get("siteEntry") or [] if isinstance(entry, dict)]

    async def query(self, site_url: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        """``searchAnalytics.query``; ``site_url`` is used verbatim."""
        data = await self._request(
            f"{API_BASE}/sites/{quote(site_url, safe='')}/searchAnalytics/query", body
        )
        return [row for row in data.get("rows") or [] if isinstance(row, dict)]

    async def inspect(
        self, site_url: str, url: str, language_code: str | None = None
    ) -> dict[str, Any] | None:
        """URL Inspection: Google's stored index status for one URL; not a live crawl."""
        body: dict[str, Any] = {"siteUrl": site_url, "inspectionUrl": url}
        if language_code:
            body["languageCode"] = language_code
        result = (await self._request(INSPECT_URL, body)).get("inspectionResult")
        return result if isinstance(result, dict) else None
