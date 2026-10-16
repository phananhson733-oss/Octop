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
    service = SimpleNamespace(
        project_for_agent=AsyncMock(return_value={"project_id": "project-a"}),
        report_context=AsyncMock(
            return_value={
                "project": {"project_id": "project-a"},
                "run": run,
                "saved_keywords": [],
                "context_hash": "a" * 64,
                "instructions": "Use the evidence.",
            }
        ),
        save_report=AsyncMock(return_value={"report_id": "report-a"}),
        keyword_rows=AsyncMock(return_value=[]),
        get_run=AsyncMock(return_value=run),
        list_runs=AsyncMock(return_value=[]),
        list_reports=AsyncMock(return_value=[]),
    )
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
    service.report_context.return_value = {
        "project": {"project_id": "project-a"},
        "run": run,
        "saved_keywords": saved,
        "context_hash": "b" * 64,
        "instructions": "Use the evidence.",
    }
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

    for name, args, key, total, identity in [
        ("seo_list_research_rows", {"run_id": "run-a"}, "rows", 2500, "keyword"),
        ("seo_list_saved_keywords", {}, "rows", 500, "keyword"),
        ("seo_list_research_runs", {}, "runs", 300, None),
        ("seo_list_reports", {}, "reports", 100, "report_id"),
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
