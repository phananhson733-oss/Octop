"""Google Analytics 4: property discovery, report requests, normalization, organic overview.

Ported from OpenSEO d7ea785 ``server/lib/ga4Client.ts`` and ``features/ga4/services/``
(``Ga4ReportDefinitions``, ``Ga4ReportNormalization``, ``Ga4Dates``,
``Ga4OrganicOverviewService`` and the date rules of ``Ga4ReportingService``). Not ported:
previous-period comparison of breakdown reports, the attribution, ecommerce and site-search
diagnostics, search opportunities and measurement health. Analytics is free first-party
data, so nothing here is metered or cached.
"""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from octop.infra.seo.google.gsc import request_json

ADMIN_API = "https://analyticsadmin.googleapis.com/v1beta"
DATA_API = "https://analyticsdata.googleapis.com/v1beta"
PROPERTY_ID = re.compile(r"^properties/\d+$")
_MAX_SUMMARY_PAGES = 100
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
# Rows for a daily or weekly trend; 16 months of days is about 490.
_TREND_ROWS = 1000
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INTEGER = re.compile(r"^-?\d+$")
# A sharp drop in organic key events is flagged only from this many in the previous period.
_KEY_EVENT_MIN_PREVIOUS = 5
_KEY_EVENT_DROP = -0.5

REPORT_KINDS = (
    "landing_pages",
    "page_performance",
    "key_events",
    "traffic_acquisition",
    "ecommerce_performance",
    "site_search",
    "audience_breakdown",
)
CHANNELS = ("organic_search", "all")
_DEFINITIONS: dict[str, dict[str, Any]] = {
    "landing_pages": {
        "dimensions": ("hostName", "landingPage"),
        "metrics": (
            "sessions",
            "activeUsers",
            "engagedSessions",
            "engagementRate",
            "keyEvents",
            "sessionKeyEventRate",
            "transactions",
            "purchaseRevenue",
        ),
        "order": "sessions",
    },
    "page_performance": {
        "dimensions": ("hostName", "pagePath"),
        "metrics": ("screenPageViews", "activeUsers", "userEngagementDuration", "keyEvents"),
        "order": "screenPageViews",
    },
    "key_events": {
        "dimensions": ("eventName",),
        "metrics": ("keyEvents", "totalUsers"),
        "order": "keyEvents",
    },
    "traffic_acquisition": {
        "dimensions": ("sessionDefaultChannelGroup",),
        "metrics": (
            "sessions",
            "activeUsers",
            "engagedSessions",
            "engagementRate",
            "keyEvents",
            "transactions",
            "purchaseRevenue",
        ),
        "order": "sessions",
    },
    "ecommerce_performance": {
        "dimensions": ("itemName", "itemId"),
        "metrics": ("itemsViewed", "itemsAddedToCart", "itemsPurchased", "itemRevenue"),
        "order": "itemRevenue",
    },
    "site_search": {
        "dimensions": ("searchTerm",),
        "metrics": ("eventCount", "activeUsers", "sessions", "engagedSessions", "engagementRate"),
        "order": "eventCount",
    },
    "audience_breakdown": {
        "dimensions": ("deviceCategory",),
        "metrics": ("activeUsers", "sessions", "engagementRate", "keyEvents"),
        "order": "activeUsers",
    },
}
_ECOMMERCE_LANDING = {
    "dimensions": ("hostName", "landingPage"),
    "metrics": ("sessions", "transactions", "purchaseRevenue"),
    "order": "purchaseRevenue",
}
_ACQUISITION = {
    "channel_group": "sessionDefaultChannelGroup",
    "source_medium": "sessionSourceMedium",
    "campaign": "sessionCampaignName",
}
_AUDIENCE = {"device": "deviceCategory", "country": "country", "new_vs_returning": "newVsReturning"}
OVERVIEW_METRICS = (
    "sessions",
    "activeUsers",
    "engagedSessions",
    "engagementRate",
    "keyEvents",
    "transactions",
    "purchaseRevenue",
)


class Ga4Malformed(Exception):
    """Analytics answered with a report that does not match the request."""


class Ga4Invalid(ValueError):
    """The requested date range cannot be served."""


def _string_filter(field: str, value: str) -> dict[str, Any]:
    return {"filter": {"fieldName": field, "stringFilter": {"matchType": "EXACT", "value": value}}}


def _positive(field: str) -> dict[str, Any]:
    return {
        "filter": {
            "fieldName": field,
            "numericFilter": {"operation": "GREATER_THAN", "value": {"doubleValue": 0}},
        }
    }


def organic_filter() -> dict[str, Any]:
    return _string_filter("sessionDefaultChannelGroup", "Organic Search")


def _definition(kind: str, ecommerce_breakdown: str | None) -> dict[str, Any]:
    if kind == "ecommerce_performance" and ecommerce_breakdown == "landing_page":
        return _ECOMMERCE_LANDING
    return _DEFINITIONS[kind]


def report_configuration(
    *,
    kind: str,
    include_date: bool = False,
    breakdown: str | None = None,
    acquisition_breakdown: str | None = None,
    ecommerce_breakdown: str | None = None,
    audience_breakdown: str | None = None,
) -> dict[str, Any]:
    """The dimensions, metrics and effective breakdown of one report kind."""
    definition = _definition(kind, ecommerce_breakdown)
    dimensions = list(definition["dimensions"])
    if kind == "traffic_acquisition":
        dimensions = [_ACQUISITION[acquisition_breakdown or "channel_group"]]
        effective = acquisition_breakdown or "channel_group"
    elif kind == "audience_breakdown":
        dimensions = [_AUDIENCE[audience_breakdown or "device"]]
        effective = audience_breakdown or "device"
    elif kind == "page_performance":
        if include_date:
            dimensions.append("date")
        effective = "page_and_date" if include_date else "page"
    elif kind == "key_events":
        if breakdown == "event_and_landing_page":
            dimensions += ["hostName", "landingPage"]
        effective = breakdown or "event"
    elif kind == "ecommerce_performance":
        effective = ecommerce_breakdown or "item"
    elif kind == "site_search":
        effective = "search_term"
    else:
        effective = "landing_page"
    return {
        "report_kind": kind,
        "breakdown": effective,
        "dimensions": dimensions,
        "metrics": list(definition["metrics"]),
        "order": definition["order"],
    }


def report_request(
    *,
    kind: str,
    start_date: str,
    end_date: str,
    channel: str = "organic_search",
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    include_date: bool = False,
    breakdown: str | None = None,
    acquisition_breakdown: str | None = None,
    ecommerce_breakdown: str | None = None,
    only_with_transactions: bool = False,
    audience_breakdown: str | None = None,
) -> dict[str, Any]:
    """The Data API ``runReport`` body for one report kind."""
    config = report_configuration(
        kind=kind,
        include_date=include_date,
        breakdown=breakdown,
        acquisition_breakdown=acquisition_breakdown,
        ecommerce_breakdown=ecommerce_breakdown,
        audience_breakdown=audience_breakdown,
    )
    body: dict[str, Any] = {
        "dateRanges": [{"startDate": start_date, "endDate": end_date}],
        "dimensions": [{"name": name} for name in config["dimensions"]],
        "metrics": [{"name": name} for name in config["metrics"]],
        "offset": str(offset),
        "limit": str(limit),
        "orderBys": [{"metric": {"metricName": config["order"]}, "desc": True}],
        "keepEmptyRows": False,
        "returnPropertyQuota": True,
    }
    if kind == "site_search":
        # Search terms come from the view_search_results event; "(not set)" is no term.
        body["dimensionFilter"] = {
            "andGroup": {
                "expressions": [
                    _string_filter("eventName", "view_search_results"),
                    {"notExpression": _string_filter("searchTerm", "(not set)")},
                ]
            }
        }
    elif channel == "organic_search":
        body["dimensionFilter"] = organic_filter()
    if kind == "key_events":
        body["metricFilter"] = _positive("keyEvents")
    elif (
        kind == "ecommerce_performance"
        and ecommerce_breakdown == "landing_page"
        and only_with_transactions
    ):
        body["metricFilter"] = _positive("transactions")
    return body


def overview_request(start_date: str, end_date: str, trend: str | None = None) -> dict[str, Any]:
    """Organic totals (one row), or a daily / weekly organic trend."""
    dimensions = [{"name": "date" if trend == "daily" else "yearWeek"}] if trend else []
    return {
        "dateRanges": [{"startDate": start_date, "endDate": end_date}],
        "dimensions": dimensions,
        "metrics": [{"name": name} for name in OVERVIEW_METRICS],
        "dimensionFilter": organic_filter(),
        "offset": "0",
        "limit": str(_TREND_ROWS) if trend else "1",
        "orderBys": [{"dimension": {"dimensionName": dimensions[0]["name"]}}] if trend else [],
        "keepEmptyRows": False,
        "returnPropertyQuota": True,
    }


def _metric(value: Any) -> int | float:
    text = str(value)
    if _INTEGER.match(text):
        return int(text)
    try:
        number = float(text)
    except ValueError as exc:
        raise Ga4Malformed from exc
    if not math.isfinite(number):
        raise Ga4Malformed
    return number


def normalize(response: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """Rows keyed by dimension and metric name; restricted metrics become ``None``."""
    expected_dimensions = [d["name"] for d in request["dimensions"]]
    expected_metrics = [m["name"] for m in request["metrics"]]
    dimension_headers = response.get("dimensionHeaders")
    metric_headers = response.get("metricHeaders")
    raw_rows = response.get("rows") or []
    dimensions = [str(h.get("name")) for h in dimension_headers or []]
    metrics = [str(h.get("name")) for h in metric_headers or []]
    # With no data on record for the window (e.g. before the property existed) Analytics
    # leaves the headers out instead of echoing them with zero rows.
    headerless = dimension_headers is None and metric_headers is None and not raw_rows
    if not headerless and (dimensions != expected_dimensions or metrics != expected_metrics):
        raise Ga4Malformed
    meta = response.get("metadata") or {}
    restricted = [
        {
            "metric_name": item.get("metricName"),
            "restricted_metric_types": item.get("restrictedMetricTypes") or [],
        }
        for item in (meta.get("schemaRestrictionResponse") or {}).get("activeMetricRestrictions")
        or []
    ]
    restricted_names = {item["metric_name"] for item in restricted}
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        dimension_values = raw.get("dimensionValues") or []
        metric_values = raw.get("metricValues") or []
        if len(dimension_values) != len(dimensions) or len(metric_values) != len(metrics):
            raise Ga4Malformed
        row: dict[str, Any] = {
            name: str(value.get("value") or "")
            for name, value in zip(dimensions, dimension_values, strict=True)
        }
        for name, value in zip(metrics, metric_values, strict=True):
            row[name] = None if name in restricted_names else _metric(value.get("value"))
        rows.append(row)
    sampling = [
        {
            "samples_read_count": item.get("samplesReadCount"),
            "sampling_space_size": item.get("samplingSpaceSize"),
        }
        for item in meta.get("samplingMetadatas") or []
    ]
    data_loss = bool(meta.get("dataLossFromOtherRow"))
    thresholded = bool(meta.get("subjectToThresholding"))
    return {
        "rows": rows,
        "total_row_count": int(response.get("rowCount") or len(rows)),
        "metadata": {
            "data_loss_from_other_row": data_loss,
            "subject_to_thresholding": thresholded,
            "sampling": sampling,
            "restricted_metrics": restricted,
            "empty_reason": meta.get("emptyReason"),
            # Thresholded, sampled, "(other)"-folded or restricted data is not the full picture.
            "has_limited_data": data_loss or thresholded or bool(sampling) or bool(restricted),
        },
        "quota": response.get("propertyQuota"),
    }


def _day(value: str) -> date | None:
    if not _DAY.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def today_in(time_zone: str, now: float) -> date:
    """The calendar day at ``now`` in the property's time zone (UTC if Python lacks it)."""
    try:
        zone: Any = ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = UTC
    return datetime.fromtimestamp(now, zone).date()


def resolve_date_range(
    *, start_date: str | None, end_date: str | None, time_zone: str, now: float
) -> dict[str, Any]:
    """Explicit dates, or the 28 days ending yesterday in the property's time zone.

    An end after the last complete day is pulled back to it, with a warning.
    """
    if bool(start_date) != bool(end_date):
        raise Ga4Invalid("Provide both start_date and end_date, or neither.")
    last_complete = today_in(time_zone, now) - timedelta(days=1)
    requested = None
    resolved_end = last_complete
    resolved_start = last_complete - timedelta(days=27)
    if start_date and end_date:
        start, end = _day(start_date), _day(end_date)
        if start is None or end is None or start > end:
            raise Ga4Invalid(
                "Dates must be valid YYYY-MM-DD values with start_date on or before end_date."
            )
        requested = {"start_date": start_date, "end_date": end_date}
        resolved_start, resolved_end = start, end
    warnings: list[str] = []
    if resolved_end > last_complete:
        resolved_end = last_complete
        warnings.append("end_date_clamped")
    if resolved_start > resolved_end:
        raise Ga4Invalid("The resolved start_date is after the last complete Analytics day.")
    return {
        "requested": requested,
        "resolved": {
            "start_date": resolved_start.isoformat(),
            "end_date": resolved_end.isoformat(),
        },
        "warnings": warnings,
    }


def previous_period(start_date: str, end_date: str) -> dict[str, str]:
    """The equal-length window that ends the day before ``start_date`` (both inclusive)."""
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    previous_end = start - timedelta(days=1)
    return {
        "start_date": (previous_end - (end - start)).isoformat(),
        "end_date": previous_end.isoformat(),
    }


def comparison_value(current: float | None, previous: float | None) -> dict[str, Any]:
    change = current - previous if current is not None and previous is not None else None
    return {
        "current": current,
        "previous": previous,
        "absolute_change": change,
        "percent_change": change / previous if change is not None and previous else None,
    }


def _number(row: dict[str, Any] | None, metric: str) -> float | None:
    value = (row or {}).get(metric)
    return value if isinstance(value, int | float) else None


def overview(
    current: dict[str, Any], previous: dict[str, Any], trend: dict[str, Any]
) -> dict[str, Any]:
    """Totals against the previous period, the trend, and a sharp key-event drop."""
    now = current["rows"][0] if current["rows"] else None
    before = previous["rows"][0] if previous["rows"] else None
    limited = any(r["metadata"]["has_limited_data"] for r in (current, previous, trend))
    diagnostics: list[dict[str, Any]] = []
    events_now, events_before = _number(now, "keyEvents"), _number(before, "keyEvents")
    if (
        not limited
        and events_now is not None
        and events_before is not None
        and events_before >= _KEY_EVENT_MIN_PREVIOUS
        and (events_now - events_before) / events_before <= _KEY_EVENT_DROP
    ):
        diagnostics.append(
            {
                "code": "key_events_sharp_decline",
                "severity": "warning",
                "message": "Organic key events declined sharply compared with the previous "
                "equal-length period.",
                "evidence": {
                    "current": events_now,
                    "previous": events_before,
                    "percent_change": (events_now - events_before) / events_before,
                },
                "threshold": {
                    "minimum_previous_key_events": _KEY_EVENT_MIN_PREVIOUS,
                    "percent_change": _KEY_EVENT_DROP,
                },
            }
        )
    return {
        "current": now,
        "previous": before,
        "comparison": {
            metric: comparison_value(_number(now, metric), _number(before, metric))
            for metric in OVERVIEW_METRICS
        },
        "trend": trend["rows"],
        "diagnostics": diagnostics,
        "has_limited_data": limited,
        "trend_truncated": trend["total_row_count"] > len(trend["rows"]),
        "quota": trend["quota"] or current["quota"],
    }


class Ga4Client:
    """Admin API discovery and Data API reports with a token from ``token``."""

    def __init__(
        self,
        token: Callable[[], Awaitable[str]],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._transport = transport

    async def _request(self, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return await request_json(self._token, url, body, transport=self._transport)

    async def list_properties(self) -> list[dict[str, str]]:
        """Every property the grant can read, across its Analytics accounts."""
        properties: list[dict[str, str]] = []
        page_token = ""
        for _page in range(_MAX_SUMMARY_PAGES):
            query = {"pageSize": "200"} | ({"pageToken": page_token} if page_token else {})
            url = f"{ADMIN_API}/accountSummaries?{urlencode(query)}"
            data = await self._request(url)
            for account in data.get("accountSummaries") or []:
                for prop in account.get("propertySummaries") or []:
                    if PROPERTY_ID.match(str(prop.get("property") or "")):
                        properties.append(
                            {
                                "property_id": prop["property"],
                                "display_name": str(prop.get("displayName") or ""),
                                "account_display_name": str(account.get("displayName") or ""),
                            }
                        )
            page_token = str(data.get("nextPageToken") or "")
            if not page_token:
                return properties
        raise Ga4Malformed

    async def get_property(self, property_id: str) -> dict[str, str]:
        """Name, display name, time zone and currency of one property."""
        if not PROPERTY_ID.match(property_id):
            raise ValueError(property_id)
        data = await self._request(f"{ADMIN_API}/{property_id}")
        if not PROPERTY_ID.match(str(data.get("name") or "")) or not data.get("timeZone"):
            raise Ga4Malformed
        return {
            "property_id": str(data["name"]),
            "display_name": str(data.get("displayName") or ""),
            "time_zone": str(data["timeZone"]),
            "currency_code": str(data.get("currencyCode") or ""),
        }

    async def run_report(self, property_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if not PROPERTY_ID.match(property_id):
            raise ValueError(property_id)
        return await self._request(f"{DATA_API}/{property_id}:runReport", body)
