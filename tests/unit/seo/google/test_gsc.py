"""Search Console request and report shaping, ported from OpenSEO d7ea785's own tests
(searchAnalytics.test.ts, searchPerformanceReport.test.ts) with the same cases."""

from datetime import date

import pytest

from octop.infra.seo.google.gsc import (
    GoogleApiError,
    build_request,
    previous_period,
    resolve_date_range,
    striking_distance,
    sum_totals,
)

TODAY = date(2026, 5, 28)


def test_28_day_window_ends_three_days_back():
    assert resolve_date_range(date_range="last_28_days", today=TODAY) == (
        "2026-04-27",
        "2026-05-25",
    )


def test_start_is_clamped_to_the_16_month_floor():
    # end (2026-05-25) - 16 months = 2025-01-25, but the floor is today - 16 months.
    assert resolve_date_range(date_range="last_16_months", today=TODAY)[0] == "2025-01-28"


def test_explicit_dates_pass_through_with_only_an_old_start_clamped():
    assert resolve_date_range(start_date="2020-01-01", end_date="2026-05-01", today=TODAY) == (
        "2025-01-28",
        "2026-05-01",
    )
    assert (
        resolve_date_range(start_date="2026-01-01", end_date="2026-05-01", today=TODAY)[0]
        == "2026-01-01"
    )


@pytest.mark.parametrize(
    ("date_range", "today", "start", "end"),
    [
        ("last_3_months", date(2026, 6, 3), "2026-02-28", "2026-05-31"),
        ("last_16_months", date(2026, 6, 30), "2025-02-28", "2026-06-27"),
    ],
)
def test_calendar_months_do_not_overflow_short_months(date_range, today, start, end):
    assert resolve_date_range(date_range=date_range, today=today) == (start, end)


def test_flat_filters_become_one_and_group():
    request = build_request(
        today=TODAY,
        dimensions=["query"],
        filters=[{"dimension": "page", "operator": "equals", "expression": "https://e.com/p"}],
    )
    # Search Console ignores a top-level `filters` field.
    assert "filters" not in request
    assert request["dimensionFilterGroups"] == [
        {
            "groupType": "and",
            "filters": [
                {"dimension": "page", "operator": "equals", "expression": "https://e.com/p"}
            ],
        }
    ]


def test_row_limit_is_clamped_and_defaults_are_filled():
    assert build_request(today=TODAY, row_limit=99999)["rowLimit"] == 1000
    assert build_request(today=TODAY, row_limit=0)["rowLimit"] == 1
    request = build_request(today=TODAY)
    assert (request["dimensions"], request["rowLimit"], request["type"], request["dataState"]) == (
        ["query"],
        250,
        "web",
        "all",
    )
    assert "startRow" not in request
    assert build_request(today=TODAY, start_row=250)["startRow"] == 250


def test_totals_weight_position_by_impressions():
    totals = sum_totals(
        [
            {"clicks": 10, "impressions": 100, "ctr": 0.1, "position": 2},
            {"clicks": 5, "impressions": 300, "ctr": 0.016, "position": 10},
        ]
    )
    assert (totals["clicks"], totals["impressions"]) == (15, 400)
    assert totals["ctr"] == pytest.approx(15 / 400)
    # (2*100 + 10*300) / 400 = 8
    assert totals["position"] == pytest.approx(8)
    assert sum_totals([]) == {"clicks": 0, "impressions": 0, "ctr": 0, "position": 0}


def _row(query, position, impressions, page=None):
    page = page or f"https://example.com/{query}"
    return {
        "keys": [query, page],
        "clicks": 1,
        "impressions": impressions,
        "ctr": 0.01,
        "position": position,
    }


def test_striking_distance_keeps_5_to_20_by_impressions():
    rows = striking_distance(
        [
            _row("top-spot", 2, 900),
            _row("low-edge", 5, 10),
            _row("close", 6.4, 100),
            _row("closer", 11, 400),
            _row("high-edge", 20, 20),
            _row("page-3", 24, 800),
        ]
    )
    assert [row["query"] for row in rows] == ["closer", "close", "high-edge", "low-edge"]


def test_a_query_whose_best_page_ranks_above_the_band_is_dropped():
    assert (
        striking_distance(
            [
                _row("openseo", 2, 900, "https://x.com/home"),
                _row("openseo", 6, 300, "https://x.com/mcp"),
            ]
        )
        == []
    )


def test_a_query_collapses_to_its_best_page_in_band():
    rows = striking_distance(
        [_row("kw", 14, 100, "https://x.com/a"), _row("kw", 8, 500, "https://x.com/b")]
    )
    assert [(row["page"], row["position"]) for row in rows] == [("https://x.com/b", 8)]


@pytest.mark.parametrize(
    ("start", "end", "prev_start", "prev_end"),
    [
        ("2026-06-01", "2026-06-28", "2026-05-04", "2026-05-31"),
        ("2026-06-10", "2026-06-10", "2026-06-09", "2026-06-09"),
    ],
)
def test_previous_period_is_the_same_length_ending_the_day_before(start, end, prev_start, prev_end):
    assert previous_period(start, end) == (prev_start, prev_end)


def test_disabled_api_is_not_a_dead_grant():
    disabled = GoogleApiError(
        403, '{"error": {"status": "PERMISSION_DENIED", "details": "SERVICE_DISABLED"}}'
    )
    assert disabled.api_disabled and not disabled.denied
    assert GoogleApiError(403, "{}").denied and GoogleApiError(401, "").denied
    assert not GoogleApiError(429, "").denied
