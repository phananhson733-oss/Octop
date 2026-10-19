"""GA4 request shapes, normalization, dates and the organic overview.

Cases ported from OpenSEO d7ea785 ``Ga4ReportingService.test.ts`` and
``Ga4OrganicOverviewService.test.ts`` (same inputs, same expectations).
"""

from datetime import UTC, datetime

import pytest

from octop.infra.seo.google import ga4

# 2026-08-06T15:00:00Z, property in New York: yesterday there is 2026-08-05.
NOW = datetime(2026, 8, 6, 15, tzinfo=UTC).timestamp()
NY = "America/New_York"
LANDING = {
    "dimensionHeaders": [{"name": "hostName"}, {"name": "landingPage"}],
    "metricHeaders": [
        {"name": n}
        for n in (
            "sessions",
            "activeUsers",
            "engagedSessions",
            "engagementRate",
            "keyEvents",
            "sessionKeyEventRate",
            "transactions",
            "purchaseRevenue",
        )
    ],
}


def _values(*values):
    return [{"value": v} for v in values]


def _landing_request(**kwargs):
    return ga4.report_request(
        kind="landing_pages", start_date="2026-07-09", end_date="2026-08-05", **kwargs
    )


def test_landing_pages_ask_for_organic_sessions_with_string_paging():
    body = _landing_request(limit=1, offset=1)
    assert body["dateRanges"] == [{"startDate": "2026-07-09", "endDate": "2026-08-05"}]
    assert body["dimensionFilter"] == {
        "filter": {
            "fieldName": "sessionDefaultChannelGroup",
            "stringFilter": {"matchType": "EXACT", "value": "Organic Search"},
        }
    }
    assert (body["offset"], body["limit"]) == ("1", "1")
    assert body["orderBys"] == [{"metric": {"metricName": "sessions"}, "desc": True}]
    assert "metricFilter" not in body


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {"kind": "site_search", "channel": "all"},
            {
                "dimensionFilter": {
                    "andGroup": {
                        "expressions": [
                            {
                                "filter": {
                                    "fieldName": "eventName",
                                    "stringFilter": {
                                        "matchType": "EXACT",
                                        "value": "view_search_results",
                                    },
                                }
                            },
                            {
                                "notExpression": {
                                    "filter": {
                                        "fieldName": "searchTerm",
                                        "stringFilter": {
                                            "matchType": "EXACT",
                                            "value": "(not set)",
                                        },
                                    }
                                }
                            },
                        ]
                    }
                }
            },
        ),
        (
            {"kind": "page_performance", "channel": "all", "include_date": True},
            {"dimensions": [{"name": "hostName"}, {"name": "pagePath"}, {"name": "date"}]},
        ),
        (
            {"kind": "key_events"},
            {
                "metricFilter": {
                    "filter": {
                        "fieldName": "keyEvents",
                        "numericFilter": {
                            "operation": "GREATER_THAN",
                            "value": {"doubleValue": 0},
                        },
                    }
                }
            },
        ),
        (
            {
                "kind": "ecommerce_performance",
                "ecommerce_breakdown": "landing_page",
                "only_with_transactions": True,
            },
            {
                "dimensions": [{"name": "hostName"}, {"name": "landingPage"}],
                "metricFilter": {
                    "filter": {
                        "fieldName": "transactions",
                        "numericFilter": {
                            "operation": "GREATER_THAN",
                            "value": {"doubleValue": 0},
                        },
                    }
                },
            },
        ),
        (
            {"kind": "traffic_acquisition", "acquisition_breakdown": "source_medium"},
            {"dimensions": [{"name": "sessionSourceMedium"}]},
        ),
        (
            {"kind": "audience_breakdown", "audience_breakdown": "new_vs_returning"},
            {"dimensions": [{"name": "newVsReturning"}]},
        ),
    ],
)
def test_report_kinds_shape_their_requests(kwargs, expected):
    body = ga4.report_request(start_date="2026-07-09", end_date="2026-08-05", **kwargs)
    for key, value in expected.items():
        assert body[key] == value
    if kwargs.get("channel") == "all" and kwargs["kind"] != "site_search":
        assert "dimensionFilter" not in body


def test_configuration_names_the_effective_breakdown():
    config = ga4.report_configuration(kind="key_events", breakdown="event_and_landing_page")
    assert config["breakdown"] == "event_and_landing_page"
    assert config["dimensions"] == ["eventName", "hostName", "landingPage"]
    assert ga4.report_configuration(kind="landing_pages")["breakdown"] == "landing_page"


def test_normalize_keys_rows_and_flags_limited_data():
    request = _landing_request(limit=1, offset=1)
    report = ga4.normalize(
        LANDING
        | {
            "rows": [
                {
                    "dimensionValues": _values("example.com", "/guides/seo"),
                    "metricValues": _values("20", "18", "14", "0.7", "3", "0.15", "1", "99.5"),
                }
            ],
            "rowCount": 3,
            "metadata": {
                "subjectToThresholding": True,
                "samplingMetadatas": [{"samplesReadCount": "1000", "samplingSpaceSize": "10000"}],
            },
            "propertyQuota": {"tokensPerDay": {"consumed": 10, "remaining": 90}},
        },
        request,
    )
    assert report["rows"][0] | {} == {
        "hostName": "example.com",
        "landingPage": "/guides/seo",
        "sessions": 20,
        "activeUsers": 18,
        "engagedSessions": 14,
        "engagementRate": 0.7,
        "keyEvents": 3,
        "sessionKeyEventRate": 0.15,
        "transactions": 1,
        "purchaseRevenue": 99.5,
    }
    assert report["total_row_count"] == 3
    assert report["metadata"]["has_limited_data"] is True
    assert report["quota"]["tokensPerDay"] == {"consumed": 10, "remaining": 90}


def test_restricted_metrics_are_null():
    report = ga4.normalize(
        LANDING
        | {
            "rows": [
                {
                    "dimensionValues": _values("example.com", "/"),
                    "metricValues": _values("1", "1", "1", "1", "1", "1", "1", "0"),
                }
            ],
            "metadata": {
                "schemaRestrictionResponse": {
                    "activeMetricRestrictions": [
                        {"metricName": "purchaseRevenue", "restrictedMetricTypes": ["COST_DATA"]}
                    ]
                }
            },
        },
        _landing_request(),
    )
    assert report["rows"][0]["purchaseRevenue"] is None
    assert report["metadata"]["restricted_metrics"] == [
        {"metric_name": "purchaseRevenue", "restricted_metric_types": ["COST_DATA"]}
    ]


def test_headerless_answers_are_empty_and_mismatched_headers_malformed():
    assert ga4.normalize({}, _landing_request())["rows"] == []
    with pytest.raises(ga4.Ga4Malformed):
        ga4.normalize(
            {"dimensionHeaders": [{"name": "pagePath"}], "metricHeaders": []}, _landing_request()
        )
    with pytest.raises(ga4.Ga4Malformed):
        ga4.normalize(
            LANDING | {"rows": [{"dimensionValues": _values("a"), "metricValues": []}]},
            _landing_request(),
        )
    with pytest.raises(ga4.Ga4Malformed):
        ga4.normalize(
            LANDING
            | {
                "rows": [
                    {
                        "dimensionValues": _values("a", "/"),
                        "metricValues": _values("x", "1", "1", "1", "1", "1", "1", "1"),
                    }
                ]
            },
            _landing_request(),
        )


def test_default_range_is_the_28_days_ending_yesterday_in_the_property_zone():
    dates = ga4.resolve_date_range(start_date=None, end_date=None, time_zone=NY, now=NOW)
    assert dates == {
        "requested": None,
        "resolved": {"start_date": "2026-07-09", "end_date": "2026-08-05"},
        "warnings": [],
    }
    # Just after midnight UTC it is still the previous day in New York.
    late = datetime(2026, 8, 7, 2, tzinfo=UTC).timestamp()
    assert (
        ga4.resolve_date_range(start_date=None, end_date=None, time_zone=NY, now=late)["resolved"][
            "end_date"
        ]
        == "2026-08-05"
    )


def test_a_future_end_is_clamped_and_a_long_start_kept():
    dates = ga4.resolve_date_range(
        start_date="2025-01-01", end_date="2026-08-20", time_zone=NY, now=NOW
    )
    assert dates["resolved"] == {"start_date": "2025-01-01", "end_date": "2026-08-05"}
    assert dates["warnings"] == ["end_date_clamped"]


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-07-01", None),
        ("2026-07-20", "2026-07-01"),
        ("2026-02-30", "2026-03-01"),
        ("2026-8-1", "2026-08-02"),
    ],
)
def test_half_reversed_or_invalid_ranges_are_rejected(start, end):
    with pytest.raises(ga4.Ga4Invalid):
        ga4.resolve_date_range(start_date=start, end_date=end, time_zone=NY, now=NOW)


def test_a_range_wholly_after_yesterday_is_rejected():
    with pytest.raises(ga4.Ga4Invalid):
        ga4.resolve_date_range(
            start_date="2026-08-10", end_date="2026-08-12", time_zone=NY, now=NOW
        )


def test_previous_period_has_the_same_number_of_days():
    assert ga4.previous_period("2026-07-09", "2026-08-05") == {
        "start_date": "2026-06-11",
        "end_date": "2026-07-08",
    }
    assert ga4.previous_period("2026-08-05", "2026-08-05") == {
        "start_date": "2026-08-04",
        "end_date": "2026-08-04",
    }


def _summary(*values, dimensions=(), rows=None, row_count=1):
    request = ga4.overview_request("2026-07-09", "2026-08-05", None)
    headers = {
        "dimensionHeaders": [{"name": d} for d in dimensions],
        "metricHeaders": [{"name": m} for m in ga4.OVERVIEW_METRICS],
    }
    if dimensions:
        request = ga4.overview_request(
            "2026-07-09", "2026-08-05", "daily" if dimensions == ("date",) else "weekly"
        )
    body = headers | {"rowCount": row_count}
    if rows is not None:
        body["rows"] = rows
    elif values:
        body["rows"] = [{"dimensionValues": [], "metricValues": _values(*values)}]
    return ga4.normalize(body, request)


def test_overview_compares_equal_periods_and_flags_a_truncated_trend():
    current = _summary("100", "80", "70", "0.7", "10", "4", "500")
    previous = _summary("80", "70", "50", "0.625", "5", "2", "250")
    trend = _summary(
        dimensions=("yearWeek",),
        rows=[
            {
                "dimensionValues": _values("202631"),
                "metricValues": _values("100", "80", "70", "0.7", "10", "4", "500"),
            }
        ],
        row_count=1200,
    )
    result = ga4.overview(current, previous, trend)
    assert result["comparison"]["sessions"] == {
        "current": 100,
        "previous": 80,
        "absolute_change": 20,
        "percent_change": 0.25,
    }
    assert result["trend"][0]["yearWeek"] == "202631" and result["trend"][0]["sessions"] == 100
    assert result["diagnostics"] == []
    assert result["trend_truncated"] is True


def test_overview_flags_a_sharp_key_event_decline():
    current = _summary("100", "80", "70", "0.7", "3", "0", "0")
    previous = _summary("100", "80", "70", "0.7", "10", "0", "0")
    trend = _summary(dimensions=("date",), row_count=0)
    result = ga4.overview(current, previous, trend)
    assert [d["code"] for d in result["diagnostics"]] == ["key_events_sharp_decline"]
    assert result["diagnostics"][0]["evidence"] == {
        "current": 3,
        "previous": 10,
        "percent_change": -0.7,
    }
    assert result["diagnostics"][0]["threshold"] == {
        "minimum_previous_key_events": 5,
        "percent_change": -0.5,
    }
    assert result["trend_truncated"] is False


def test_overview_requests_order_the_trend_by_its_dimension():
    weekly = ga4.overview_request("2026-07-09", "2026-08-05", "weekly")
    assert weekly["dimensions"] == [{"name": "yearWeek"}] and weekly["limit"] == "1000"
    assert weekly["orderBys"] == [{"dimension": {"dimensionName": "yearWeek"}}]
    totals = ga4.overview_request("2026-07-09", "2026-08-05")
    assert (totals["dimensions"], totals["limit"], totals["orderBys"]) == ([], "1", [])
