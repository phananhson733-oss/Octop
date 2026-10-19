"""Public SEO tool authorization is checked on every invocation."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from octop.infra.errors import OctopError
from octop.infra.seo.tools import MAX_RESULT_CHARS, build_seo_tools


@pytest.fixture
def tool_env(monkeypatch):
    configurable = {"user": "7", "agent_id": "agent-a", "thread_id": "thread-a"}
    monkeypatch.setattr("octop.infra.seo.tools.get_config", lambda: {"configurable": configurable})
    threads = Mock()
    threads.get.return_value = SimpleNamespace(user_id=7, agent_id="agent-a")
    run = {"run_id": "run-a", "status": "completed", "results": []}
    background = {
        "sections": [],
        "missing_sections": ["business_overview"],
        "custom_sections": [],
        "competitors": [],
        "key_pages": [],
        "research_log": [],
        "research_log_limit": 20,
    }
    service = SimpleNamespace(
        project_for_agent=AsyncMock(return_value={"project_id": "project-a"}),
        report_context=AsyncMock(
            return_value={
                "project": {"project_id": "project-a"},
                "run": run,
                "saved_keywords": [],
                "project_context": background,
                "context_hash": "a" * 64,
                "instructions": "Use the evidence.",
            }
        ),
        project_context=AsyncMock(return_value=background),
        update_context=AsyncMock(return_value=background),
        list_serps=AsyncMock(return_value=[]),
        get_serp=AsyncMock(return_value={"serp_id": "serp-a", "items": []}),
        list_lookups=AsyncMock(return_value=[]),
        get_lookup=AsyncMock(return_value={"lookup_id": "lookup-a", "items": []}),
        save_report=AsyncMock(return_value={"report_id": "report-a"}),
        keyword_rows=AsyncMock(return_value=[]),
        get_run=AsyncMock(return_value=run),
        list_runs=AsyncMock(return_value=[]),
        list_reports=AsyncMock(return_value=[]),
    )
    service.ranks = SimpleNamespace(
        list_trackers=AsyncMock(return_value=[]),
        results=AsyncMock(
            return_value={"tracker": {"tracker_id": "t"}, "run": None, "compare": "7d", "rows": []}
        ),
        history=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr("octop.infra.seo.rank.SeoRankService", lambda seo: seo.ranks)
    tools = {tool.name: tool for tool in build_seo_tools(service, "agent-a", threads)}
    return configurable, threads, service, tools


async def test_report_context_is_scoped_to_the_runtime_agent(tool_env):
    _config, _threads, service, tools = tool_env
    result = await tools["seo_get_report_context"].ainvoke({"run_id": "run-a"})
    assert '"run_id": "run-a"' in result
    service.report_context.assert_awaited_once_with(7, "project-a", "run-a")


async def test_foreign_thread_cannot_access_site_data(tool_env):
    _config, threads, service, tools = tool_env
    threads.get.return_value = SimpleNamespace(user_id=8, agent_id="agent-a")
    with pytest.raises(OctopError):
        await tools["seo_list_saved_keywords"].ainvoke({})
    service.keyword_rows.assert_not_awaited()


async def test_changed_resume_context_is_rejected(tool_env):
    config, _threads, service, tools = tool_env
    await tools["seo_get_report_context"].ainvoke({"run_id": "run-a"})
    config["agent_id"] = "other-agent"
    with pytest.raises(OctopError):
        await tools["seo_get_report_context"].ainvoke({"run_id": "run-a"})
    assert service.report_context.await_count == 1


async def test_report_provenance_uses_validated_thread(tool_env):
    _config, _threads, service, tools = tool_env
    await tools["seo_save_report"].ainvoke(
        {
            "run_id": "run-a",
            "context_hash": "context-a",
            "title": "Research",
            "summary": "Evidence",
            "html": "<html></html>",
        }
    )
    service.save_report.assert_awaited_once_with(
        7,
        "project-a",
        run_id="run-a",
        context_hash="context-a",
        title="Research",
        summary="Evidence",
        html="<html></html>",
        skill="keyword-research",
        thread_id="thread-a",
    )
    assert "seo_research_keywords" not in tools  # Paid requests require the explicit UI action.


async def test_agent_context_writes_are_attributed_and_serps_are_read_only(tool_env):
    _config, _threads, service, tools = tool_env
    ops = [{"append_research_log": {"summary": "Compared CRM terms"}}]
    result = json.loads(await tools["seo_update_project_context"].ainvoke({"updates": ops}))
    service.update_context.assert_awaited_once_with(7, "project-a", ops, author="agent")
    assert result["applied"] == 1 and result["counts"]["research_log"] == 0
    await tools["seo_list_serps"].ainvoke({"keyword": "crm"})
    service.list_serps.assert_awaited_once_with(7, "project-a", keyword="crm")
    await tools["seo_list_competitor_lookups"].ainvoke({"kind": "domain"})
    service.list_lookups.assert_awaited_once_with(7, "project-a", kind="domain")
    with pytest.raises(OctopError):
        await tools["seo_list_competitor_lookups"].ainvoke({"kind": "backlinks"})
    await tools["seo_get_rank_results"].ainvoke({"tracker_id": "t", "compare": "30d"})
    service.ranks.results.assert_awaited_once_with(7, "project-a", "t", compare="30d")
    await tools["seo_get_rank_history"].ainvoke({"tracker_id": "t", "keyword_id": "k"})
    service.ranks.history.assert_awaited_once_with(7, "project-a", "t", "k")
    assert not any(word in name for name in tools for word in ("buy", "fetch", "check"))


def _row(keyword: str, volume: int | None) -> dict:
    return {
        "keyword": keyword,
        "search_volume": volume,
        "cpc": 2.5,
        "competition": 0.4,
        "keyword_difficulty": 41,
        "intent": "commercial",
        "trend": [{"year": 2026, "month": m, "search_volume": 1300} for m in range(1, 13)],
    }


async def test_every_tool_result_stays_readable_at_maximum_size(tool_env):
    """Above ~80k chars the harness hides a result behind a file; nothing may get there."""
    _config, _threads, service, tools = tool_env
    long = "long tail keyword phrase " * 6
    run = {
        "run_id": "run-a",
        "status": "completed",
        "observed_at": "2026-10-14T00:00:00+00:00",
        "inputs": {"keywords": [f"seed {n}" for n in range(5)]},
        "results": [
            {
                "seed": f"seed {n}",
                "ok": True,
                "source": "blended",
                "used_fallback": False,
                "rows": [_row(f"{long}{n}-{i}", i if i % 7 else None) for i in range(500)],
            }
            for n in range(5)
        ],
    }
    saved = [
        _row(f"{long}saved-{i}", i)
        | {
            "tags": [f"tag-{t}-" + "x" * 56 for t in range(20)],
            "observed_at": run["observed_at"],
            "source_run_id": "run-a",
        }
        for i in range(500)
    ]
    stamp = {"updated_at": run["observed_at"], "updated_by": "user"}
    background = {
        "sections": [
            {"key": key, "content": "p" * 4000} | stamp
            for key in ("business_overview", "current_goal", "positioning", "writing_preferences")
        ],
        "missing_sections": [],
        "custom_sections": [
            {"slug": f"{'s' * 57}-{i:02}", "title": "t" * 120, "content": "c" * 4000} | stamp
            for i in range(20)
        ],
        "competitors": [
            {"domain": f"{'d' * 240}{i}.com", "name": "n" * 120, "notes": "o" * 500} | stamp
            for i in range(100)
        ],
        "key_pages": [
            {"url": f"https://example.com/{'u' * 2000}{i}", "role": "money"}
            | {"topic": "t" * 200, "notes": "o" * 500}
            | stamp
            for i in range(100)
        ],
        "research_log": [
            {"log_id": f"log-{i}", "entry_date": "2026-10-14", "summary": "l" * 1000}
            | {"created_by": "agent", "created_at": run["observed_at"]}
            for i in range(20)
        ],
        "research_log_limit": 20,
    }
    service.report_context.return_value = {
        "project": {"project_id": "project-a"},
        "run": run,
        "saved_keywords": saved,
        "project_context": {
            k: v for k, v in background.items() if k not in {"research_log", "research_log_limit"}
        },
        "context_hash": "b" * 64,
        "instructions": "Use the evidence.",
    }
    service.project_context.return_value = background
    snapshot = {
        "serp_id": "serp-a",
        "keyword": long,
        "device": "desktop",
        "depth": 100,
        "status": "completed",
        "observed_at": run["observed_at"],
        "items": [
            {
                "type": "organic",
                "rank": i + 1,
                "organic_rank": i + 1,
                "title": "t" * 200,
                "url": f"https://example.com/{'u' * 2000}{i}",
                "domain": "example.com",
                "description": "d" * 400,
            }
            for i in range(100)
        ],
    }
    service.get_serp.return_value = snapshot
    service.list_serps.return_value = [
        {k: v for k, v in snapshot.items() if k != "items"}
        | {"serp_id": f"s{i}", "item_count": 100}
        for i in range(300)
    ]
    lookup = {
        "lookup_id": "lookup-a",
        "kind": "domain",
        "domain": "rival.com",
        "keywords": None,
        "status": "completed",
        "observed_at": run["observed_at"],
        "overview": {"organic_traffic": 1.5, "organic_keywords": 9000},
        "total_count": 9000,
        "items": [
            {
                "keyword": f"{long}{i}",
                "rank": i + 1,
                "organic_rank": i + 1,
                "url": f"https://rival.com/{'u' * 2000}{i}",
                "etv": 1.0,
                "search_volume": 10,
                "cpc": 0.1,
                "keyword_difficulty": 5,
                "intent": "informational",
            }
            for i in range(100)
        ],
    }
    service.get_lookup.return_value = lookup
    service.list_lookups.return_value = [
        {k: v for k, v in lookup.items() if k != "items"}
        | {"lookup_id": f"l{i}", "item_count": 100, "keywords": [long] * 200}
        for i in range(50)
    ]
    cell = {
        "position": 3,
        "url": f"https://example.com/{'u' * 2000}",
        "serp_features": ["organic", "people_also_ask", "ai_overview", "video"],
        "checked_at": run["observed_at"],
        "previous_position": 5,
        "previous_checked_at": run["observed_at"],
    }
    tracker = {"tracker_id": "t", "domain": "example.com", "keyword_count": 200}
    service.ranks.results.return_value = {
        "tracker": tracker,
        "run": {"run_id": "r", "status": "completed", "idempotency_key": "k"},
        "compare": "7d",
        "rows": [
            {"keyword_id": f"k{i}", "keyword": f"{long}{i}", "desktop": cell, "mobile": cell}
            for i in range(200)
        ],
    }
    service.ranks.history.return_value = [
        cell | {"run_id": f"r{i}", "keyword_id": "k", "keyword": long, "device": "desktop"}
        for i in range(200)
    ]
    service.ranks.list_trackers.return_value = [
        tracker | {"tracker_id": f"t{i}", "domain": "d" * 253} for i in range(20)
    ]
    service.get_run.return_value = run
    service.keyword_rows.return_value = saved
    service.list_runs.return_value = [run] * 300
    service.list_reports.return_value = [
        {"report_id": f"r{i}", "summary": "s" * 2500} for i in range(100)
    ]

    context = await tools["seo_get_report_context"].ainvoke({"run_id": "run-a"})
    assert len(context) <= MAX_RESULT_CHARS
    # The harness preview shows the first 1000 characters; the hash must be there.
    assert '"context_hash": "' + "b" * 64 in context[:1000]
    preview = json.loads(context)
    assert preview["saved_keywords"]["total_count"] == 500
    assert preview["research_rows_preview"]["rows"][0]["search_volume"] == 499
    assert 0 < preview["project_context"]["shown"] < preview["project_context"]["total_count"]
    assert preview["project_context"]["next_offset"] == preview["project_context"]["shown"]

    for name, args, key, total, identity in [
        ("seo_list_research_rows", {"run_id": "run-a"}, "rows", 2500, "keyword"),
        ("seo_list_saved_keywords", {}, "rows", 500, "keyword"),
        ("seo_list_research_runs", {}, "runs", 300, None),
        ("seo_list_reports", {}, "reports", 100, "report_id"),
        ("seo_get_project_context", {}, "entries", 244, None),
        ("seo_list_serps", {}, "serps", 300, "serp_id"),
        ("seo_get_serp", {"serp_id": "serp-a"}, "items", 100, "url"),
        ("seo_list_competitor_lookups", {}, "lookups", 50, "lookup_id"),
        ("seo_get_competitor_lookup", {"lookup_id": "lookup-a"}, "items", 100, "keyword"),
        ("seo_list_rank_trackers", {}, "trackers", 20, "tracker_id"),
        ("seo_get_rank_results", {"tracker_id": "t"}, "rows", 200, "keyword_id"),
        (
            "seo_get_rank_history",
            {"tracker_id": "t", "keyword_id": "k"},
            "snapshots",
            200,
            "run_id",
        ),
    ]:
        seen: list = []
        offset: int | None = 0
        while offset is not None:
            text = await tools[name].ainvoke(args | {"offset": offset})
            assert len(text) <= MAX_RESULT_CHARS, name
            page = json.loads(text)
            assert page["total_count"] == total
            seen.extend(page[key])
            offset = page["next_offset"]
        assert len(seen) == total, name
        if identity:
            assert len({item[identity] for item in seen}) == total, name
