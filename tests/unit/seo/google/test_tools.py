"""The expert's Search Console and Analytics tools: identity, paging, trimming."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from octop.infra.errors import OctopError
from octop.infra.seo.tools import build_seo_tools


@pytest.fixture
def gsc_tools(monkeypatch):
    configurable = {"user": "7", "agent_id": "agent-a", "thread_id": "thread-a"}
    monkeypatch.setattr("octop.infra.seo.tools.get_config", lambda: {"configurable": configurable})
    google = SimpleNamespace(
        gsc_performance=AsyncMock(),
        gsc_inspect=AsyncMock(),
        ga4_overview=AsyncMock(return_value={"comparison": {}}),
        ga4_report=AsyncMock(),
    )
    monkeypatch.setattr("octop.infra.seo.google.service.SeoGoogleService", lambda _seo: google)
    threads = Mock()
    threads.get.return_value = SimpleNamespace(user_id=7, agent_id="agent-a")
    service = SimpleNamespace(project_for_agent=AsyncMock(return_value={"project_id": "p"}))
    tools = {tool.name: tool for tool in build_seo_tools(service, "agent-a", threads)}
    return google, threads, tools


def _row(key, position=8.0):
    return {"keys": [key], "clicks": 1, "impressions": 60, "ctr": 0.0167, "position": position}


def _data(rows, *, fetched, fetch_limit, kept=None, filtered=False, start_row=0):
    request = {
        "startDate": "2027-01-01",
        "endDate": "2027-01-28",
        "dimensions": ["query"],
        "type": "web",
        "rowLimit": fetch_limit,
    }
    if start_row:
        request["startRow"] = start_row
    return {
        "site_url": "sc-domain:example.com",
        "request": request,
        "fetched": fetched,
        "fetch_limit": fetch_limit,
        "filtered": filtered,
        "rows": rows,
        "kept": len(rows) if kept is None else kept,
    }


async def test_a_short_page_has_nothing_more(gsc_tools):
    google, _threads, tools = gsc_tools
    google.gsc_performance.return_value = _data([(0, _row("a"))], fetched=1, fetch_limit=250)
    out = json.loads(await tools["seo_gsc_performance"].ainvoke({}))
    assert (out["row_count"], out["has_more"], out["next_start_row"]) == (1, False, None)
    assert google.gsc_performance.await_args.args == (7, "p")


async def test_filtered_rows_continue_after_the_whole_fetched_window(gsc_tools):
    google, _threads, tools = gsc_tools
    google.gsc_performance.return_value = _data(
        [(3, _row("a")), (700, _row("b"))],
        fetched=1000,
        fetch_limit=1000,
        filtered=True,
        start_row=1000,
    )
    out = json.loads(await tools["seo_gsc_performance"].ainvoke({"min_position": 5}))
    assert (out["has_more"], out["next_start_row"]) == (True, 2000)
    assert out["filtered"] == "1000 rows fetched, 2 matched"


async def test_rows_cut_to_fit_resume_right_after_the_last_one_returned(gsc_tools):
    google, _threads, tools = gsc_tools
    rows = [(i * 2, _row(f"{i:03d}" + "x" * 1500)) for i in range(100)]
    google.gsc_performance.return_value = _data(
        rows, fetched=400, fetch_limit=1000, filtered=True, start_row=0
    )
    text = await tools["seo_gsc_performance"].ainvoke({"min_impressions": 50})
    out = json.loads(text)
    assert 0 < out["row_count"] < 100
    assert out["has_more"]
    assert out["next_start_row"] == rows[out["row_count"] - 1][0] + 1


async def test_inspection_lists_are_cut_to_five_with_a_count(gsc_tools):
    google, _threads, tools = gsc_tools
    index = {"verdict": "PASS", "referringUrls": [f"u{i}" for i in range(8)], "sitemap": ["s"]}
    google.gsc_inspect.return_value = {
        "site_url": "sc-domain:example.com",
        "results": [
            {"url": "https://example.com/", "result": {"indexStatusResult": index}},
            {"url": "https://x.example/", "result": None, "error": "403: nope"},
        ],
    }
    out = json.loads(
        await tools["seo_gsc_inspect_urls"].ainvoke({"urls": ["https://example.com/"]})
    )
    first = out["results"][0]["result"]["indexStatusResult"]
    assert (first["referringUrls"], first["referringUrls_count"]) == (
        ["u0", "u1", "u2", "u3", "u4"],
        8,
    )
    assert first["sitemap"] == ["s"] and "sitemap_count" not in first
    assert google.gsc_inspect.await_args.kwargs == {
        "urls": ["https://example.com/"],
        "language_code": None,
    }


async def test_a_foreign_thread_reads_no_search_console_data(gsc_tools):
    google, threads, tools = gsc_tools
    threads.get.return_value = SimpleNamespace(user_id=8, agent_id="agent-a")
    with pytest.raises(OctopError):
        await tools["seo_gsc_performance"].ainvoke({})
    google.gsc_performance.assert_not_awaited()


async def test_ga4_tools_pass_blank_options_as_unset(gsc_tools):
    google, _threads, tools = gsc_tools
    await tools["seo_ga4_overview"].ainvoke({"trend": "weekly"})
    assert google.ga4_overview.await_args.kwargs == {
        "start_date": None,
        "end_date": None,
        "trend": "weekly",
    }
    google.ga4_report.return_value = {
        "rows": [],
        "row_count": 0,
        "page_info": {"offset": 0, "limit": 100, "has_more": False, "next_offset": None},
    }
    await tools["seo_ga4_report"].ainvoke({"kind": "key_events", "breakdown": ""})
    kwargs = google.ga4_report.await_args.kwargs
    assert (kwargs["kind"], kwargs["breakdown"], kwargs["channel"]) == (
        "key_events",
        None,
        "organic_search",
    )


async def test_ga4_rows_cut_to_fit_continue_after_the_last_one(gsc_tools):
    google, _threads, tools = gsc_tools
    rows = [{"landingPage": f"/{i:03d}" + "x" * 1500, "sessions": i} for i in range(100)]
    google.ga4_report.return_value = {
        "rows": rows,
        "row_count": 100,
        "page_info": {"offset": 200, "limit": 100, "has_more": False, "next_offset": None},
    }
    out = json.loads(await tools["seo_ga4_report"].ainvoke({"kind": "landing_pages"}))
    assert 0 < out["row_count"] < 100 and len(out["rows"]) == out["row_count"]
    assert out["page_info"]["has_more"] is True
    assert out["page_info"]["next_offset"] == 200 + out["row_count"]
