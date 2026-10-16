"""Public SEO tool authorization is checked on every invocation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from octop.infra.errors import OctopError
from octop.infra.seo.tools import build_seo_tools


@pytest.fixture
def tool_env(monkeypatch):
    configurable = {"user": "7", "agent_id": "agent-a", "thread_id": "thread-a"}
    monkeypatch.setattr("octop.infra.seo.tools.get_config", lambda: {"configurable": configurable})
    threads = Mock()
    threads.get.return_value = SimpleNamespace(user_id=7, agent_id="agent-a")
    service = SimpleNamespace(
        project_for_agent=AsyncMock(return_value={"project_id": "project-a"}),
        report_context=AsyncMock(return_value={"run": {"run_id": "run-a"}, "saved_keywords": []}),
        save_report=AsyncMock(return_value={"report_id": "report-a"}),
        list_keywords=AsyncMock(return_value={"rows": [], "total_count": 0}),
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
    service.list_keywords.assert_not_awaited()


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
