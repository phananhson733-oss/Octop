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
        project_for_agent=AsyncMock(return_value=(7, {"project_id": "project-a"})),
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
        list_ai_lookups=AsyncMock(return_value=[]),
        get_ai_lookup=AsyncMock(
            return_value={
                "lookup_id": "lookup-p",
                "kind": "prompt",
                "status": "unknown",
                "error": "interrupted",
                "request": {"models": ["chat_gpt"]},
                "items": [],
            }
        ),
        save_report=AsyncMock(return_value={"report_id": "report-a"}),
        keyword_rows=AsyncMock(return_value=[]),
        get_run=AsyncMock(return_value=run),
        list_runs=AsyncMock(return_value=[]),
        list_reports=AsyncMock(return_value=[]),
        save_keywords=AsyncMock(return_value={"saved": 2}),
        provider_status=AsyncMock(return_value={"configured": True}),
        repos=SimpleNamespace(secret_repo=SimpleNamespace(get_or_create=lambda _k, _f: b"k" * 32)),
    )
    service.ranks = SimpleNamespace(
        list_trackers=AsyncMock(return_value=[]),
        results=AsyncMock(
            return_value={"tracker": {"tracker_id": "t"}, "run": None, "compare": "7d", "rows": []}
        ),
        history=AsyncMock(return_value=[]),
        create_tracker=AsyncMock(return_value={"tracker": {"tracker_id": "t"}, "rows": []}),
        update_tracker=AsyncMock(return_value={"tracker": {"tracker_id": "t"}, "rows": []}),
        add_keywords=AsyncMock(return_value={"added": 1}),
        remove_keyword=AsyncMock(return_value=None),
    )
    monkeypatch.setattr("octop.infra.seo.rank.SeoRankService", lambda seo: seo.ranks)
    service.ai = SimpleNamespace(
        results=AsyncMock(
            return_value={
                "settings": {"engines": ["chatgpt"]},
                "prompts": [],
                "runs": [],
                "run": None,
                "coverage": {},
                "summaries": [],
                "sources": [],
                "rows": [],
            }
        ),
        answer=AsyncMock(
            return_value={"run_id": "r", "answer_markdown": None, "citations": [], "brands": []}
        ),
        add_prompts=AsyncMock(return_value={"added": 2}),
        remove_prompt=AsyncMock(return_value=None),
        set_engines=AsyncMock(return_value={"settings": {"engines": ["gemini"]}, "prompts": []}),
    )
    monkeypatch.setattr("octop.infra.seo.ai_visibility.SeoAiService", lambda seo: seo.ai)
    service.audits = SimpleNamespace(
        cancel=AsyncMock(return_value={"audit_id": "a", "status": "cancelled"})
    )
    monkeypatch.setattr("octop.infra.seo.audit.service.SeoAuditService", lambda seo: seo.audits)
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


async def test_shared_account_reads_the_owner_data(tool_env):
    # An seo-permission account (user 7) works in the admin's (user 1) SEO data.
    _config, _threads, service, tools = tool_env
    service.project_for_agent.return_value = (1, {"project_id": "project-a"})
    await tools["seo_list_saved_keywords"].ainvoke({})
    service.project_for_agent.assert_awaited_with(7, "agent-a")
    service.keyword_rows.assert_awaited_once_with(1, "project-a", search="")


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
    assert "seo_research_keywords" not in tools  # Paid requests are only proposed.


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
        await tools["seo_list_competitor_lookups"].ainvoke({"kind": "keywords"})
    await tools["seo_list_ai_lookups"].ainvoke({"kind": "prompt"})
    service.list_ai_lookups.assert_awaited_once_with(7, "project-a", kind="prompt")
    for args in ({"lookup_id": "lookup-p", "model": "claude"}, {"lookup_id": "lookup-p"}):
        with pytest.raises(OctopError) as unasked:
            await tools["seo_get_ai_lookup"].ainvoke(args)
        assert unasked.value.code == "SEO_INVALID_INPUT"
    # An asked model without an answer reads as the lookup's status, not a missing lookup.
    pending = json.loads(
        await tools["seo_get_ai_lookup"].ainvoke({"lookup_id": "lookup-p", "model": "chat_gpt"})
    )
    assert (pending["status"], pending["error"], pending["citations"]) == (
        "unknown",
        "interrupted",
        [],
    )
    service.get_ai_lookup.return_value = {
        "lookup_id": "lookup-b",
        "kind": "brand",
        "overview": {"total_mentions": 3},
        "items": [{"section": "page"}],
    }
    brand = json.loads(await tools["seo_get_ai_lookup"].ainvoke({"lookup_id": "lookup-b"}))
    assert brand["overview"] == {"total_mentions": 3} and brand["items"] == [{"section": "page"}]
    await tools["seo_get_rank_results"].ainvoke({"tracker_id": "t", "compare": "30d"})
    service.ranks.results.assert_awaited_once_with(7, "project-a", "t", compare="30d")
    await tools["seo_get_rank_history"].ainvoke({"tracker_id": "t", "keyword_id": "k"})
    service.ranks.history.assert_awaited_once_with(7, "project-a", "t", "k")
    await tools["seo_get_ai_visibility"].ainvoke(
        {"run_id": "r", "view": "brands", "branded": "all", "gap": True}
    )
    service.ai.results.assert_awaited_once_with(7, "project-a", run_id="r", branded="all", gap=True)
    with pytest.raises(OctopError):
        await tools["seo_get_ai_visibility"].ainvoke({"view": "answers"})
    await tools["seo_get_ai_answer"].ainvoke({"run_id": "r", "prompt_id": "p", "engine": "gemini"})
    service.ai.answer.assert_awaited_once_with(7, "project-a", "r", "p", "gemini")
    # Proposals buy nothing; any other tool named after a purchase would buy it directly.
    assert not any(
        word in name
        for name in tools
        if not name.startswith("seo_propose_")
        for word in ("buy", "fetch", "check")
    )


async def test_free_writes_call_the_routes_services_on_the_owner_data(tool_env):
    # The same service calls as the page's routes, in the data owner's project.
    _config, _threads, service, tools = tool_env
    service.project_for_agent.return_value = (1, {"project_id": "project-a"})
    await tools["seo_save_keywords"].ainvoke({"run_id": "run-a", "keywords": ["crm", "erp"]})
    service.save_keywords.assert_awaited_once_with(
        1, "project-a", run_id="run-a", keywords=["crm", "erp"], tags=[], tag_mode="append"
    )
    await tools["seo_add_ai_prompts"].ainvoke({"prompts": ["best crm?"], "topic": "CRM"})
    service.ai.add_prompts.assert_awaited_once_with(
        1, "project-a", prompts=["best crm?"], topic="CRM"
    )
    await tools["seo_remove_ai_prompt"].ainvoke({"prompt_id": "p"})
    service.ai.remove_prompt.assert_awaited_once_with(1, "project-a", "p")
    engines = json.loads(await tools["seo_set_ai_engines"].ainvoke({"engines": ["gemini"]}))
    service.ai.set_engines.assert_awaited_once_with(1, "project-a", engines=["gemini"])
    assert engines == {"settings": {"engines": ["gemini"]}}
    created = json.loads(
        await tools["seo_create_rank_tracker"].ainvoke({"depth": 20, "keywords": ["crm"]})
    )
    service.ranks.create_tracker.assert_awaited_once_with(
        1, "project-a", domain=None, devices="both", depth=20, keywords=["crm"]
    )
    assert created == {"tracker": {"tracker_id": "t"}}
    await tools["seo_update_rank_tracker"].ainvoke({"tracker_id": "t", "depth": 30})
    service.ranks.update_tracker.assert_awaited_once_with(1, "project-a", "t", depth=30)
    await tools["seo_add_rank_keywords"].ainvoke({"tracker_id": "t", "keywords": ["erp"]})
    service.ranks.add_keywords.assert_awaited_once_with(1, "project-a", "t", keywords=["erp"])
    await tools["seo_remove_rank_keyword"].ainvoke({"tracker_id": "t", "keyword_id": "k"})
    service.ranks.remove_keyword.assert_awaited_once_with(1, "project-a", "t", "k")
    await tools["seo_cancel_audit"].ainvoke({"audit_id": "a"})
    service.audits.cancel.assert_awaited_once_with(1, "project-a", "a")
    # Deleting a tracker, an audit or a report drops data: those stay on the page.
    assert not any(name.startswith(("seo_delete", "seo_remove_rank_tracker")) for name in tools)


async def test_proposals_validate_like_the_routes_and_buy_nothing(tool_env):
    _config, _threads, service, tools = tool_env
    service.project_for_agent.return_value = (1, {"project_id": "project-a"})
    first = json.loads(
        await tools["seo_propose_competitor_lookup"].ainvoke(
            {"kind": "backlinks", "domain": "rival.com"}
        )
    )
    purchase = first["purchase"]
    assert first["status"] == "awaiting_confirmation"
    assert (purchase["kind"], purchase["project_id"]) == ("lookup", "project-a")
    # Validated and dumped as the route reads it, without the key or the confirmation.
    assert purchase["input"] == {"kind": "backlinks", "domain": "rival.com"}
    assert purchase["idempotency_key"].startswith("agent-")
    # Signed whole, so the card can only buy what this tool returned.
    assert len(first["signature"]) == 64
    service.provider_status.assert_awaited_with(1)
    again = json.loads(
        await tools["seo_propose_competitor_lookup"].ainvoke(
            {"kind": "backlinks", "domain": "rival.com"}
        )
    )
    assert again["purchase"]["idempotency_key"] != purchase["idempotency_key"]
    prompt = json.loads(
        await tools["seo_propose_prompt_lookup"].ainvoke(
            {"prompt": "  best crm?  ", "models": ["claude", "gemini"]}
        )
    )
    assert prompt["purchase"]["input"] == {
        "prompt": "best crm?",
        "models": ["claude", "gemini"],
        "web_search": True,
    }
    for name, args in (
        ("seo_propose_research", {"keywords": ["a", "b", "c", "d", "e", "f"]}),
        ("seo_propose_serp", {"keyword": "crm", "depth": 50}),
        ("seo_propose_competitor_lookup", {"kind": "competitors", "domain": "rival.com"}),
        ("seo_propose_brand_lookup", {"target": "acme", "competitors": list("abcdef")}),
    ):
        with pytest.raises(OctopError) as invalid:
            await tools[name].ainvoke(args)
        assert invalid.value.code == "SEO_INVALID_INPUT", name
    service.provider_status.return_value = {"configured": False}
    with pytest.raises(OctopError) as missing:
        await tools["seo_propose_serp"].ainvoke({"keyword": "crm"})
    assert missing.value.code == "SEO_PROVIDER_REQUIRED"


async def test_check_proposals_carry_the_server_estimate(tool_env):
    _config, _threads, service, tools = tool_env
    rows = [{"keyword_id": f"k{n}", "keyword": f"crm {n}"} for n in range(25)]
    service.ranks.results.return_value = {
        "tracker": {"tracker_id": "t", "domain": "acme.com", "devices": "both", "depth": 20}
        | {"keyword_count": 25, "estimate_usd": 0.1},
        "run": None,
        "rows": rows,
    }
    rank = json.loads(await tools["seo_propose_rank_check"].ainvoke({"tracker_id": "t"}))
    service.ranks.results.assert_awaited_with(7, "project-a", "t")
    assert rank["purchase"]["input"] == {"max_cost_usd": 0.1}
    assert (rank["purchase"]["tracker_id"], rank["purchase"]["checks"]) == ("t", 50)
    # The card lists the first keywords; the fingerprint covers all of them.
    assert rank["purchase"]["keywords"] == [f"crm {n}" for n in range(20)]
    assert rank["purchase"]["keyword_count"] == 25
    fingerprint = rank["purchase"]["content_hash"]
    rows[24]["keyword"] = "erp"
    again = json.loads(await tools["seo_propose_rank_check"].ainvoke({"tracker_id": "t"}))
    assert again["purchase"]["content_hash"] != fingerprint
    prompts = [{"prompt_id": f"p{n}", "prompt": f"best crm {n}?"} for n in (1, 2, 3)]
    service.ai.results.return_value = {
        "settings": {"engines": ["chatgpt", "gemini"], "estimate_usd": 0.024}
        | {"location_code": 2840, "language_code": "en"},
        "prompts": prompts,
    }
    check = json.loads(await tools["seo_propose_ai_check"].ainvoke({}))
    assert check["purchase"]["input"] == {"max_cost_usd": 0.024}
    assert check["purchase"]["answers"] == 6
    assert check["purchase"]["prompts"] == ["best crm 1?", "best crm 2?", "best crm 3?"]
    assert len(check["purchase"]["content_hash"]) == 64
    service.ranks.results.return_value["tracker"]["keyword_count"] = 0
    service.ai.results.return_value["prompts"] = []
    for name, args in (
        ("seo_propose_rank_check", {"tracker_id": "t"}),
        ("seo_propose_ai_check", {}),
    ):
        with pytest.raises(OctopError) as empty:
            await tools[name].ainvoke(args)
        assert empty.value.code == "SEO_INVALID_INPUT", name


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
    brands = [{"name": "Example", "domain": "example.com", "own": True}] + [
        {"name": "n" * 120, "domain": f"{'c' * 56}{i:02d}.com", "own": False} for i in range(100)
    ]
    matches = [
        b | {"mentioned": True, "cited": True, "first_mention": i} for i, b in enumerate(brands)
    ]
    ai_run = {"run_id": "r", "status": "completed", "started_at": "x", "engines": ["chatgpt"]}
    service.ai.results.return_value = {
        "settings": {"engines": ["chatgpt"], "prompt_count": 100},
        "prompts": [
            {"prompt_id": f"p{i}", "topic": "t" * 100, "prompt": long * 10, "branded": False}
            for i in range(100)
        ],
        "runs": [ai_run | {"run_id": f"r{i}", "brands": brands} for i in range(20)],
        "run": ai_run | {"brands": brands, "answers_total": 300},
        "coverage": {"answered": 300, "no_answer": 0, "failed": 0},
        "summaries": [
            b | {"answers": 300, "mentions": 300, "citations": 300, "avg_position": 1.5}
            for b in brands
        ],
        "sources": [
            {"domain": "d" * 253, "citations": 9, "answers": 9, "owner": None} for _ in range(50)
        ],
        "rows": [
            {
                "prompt_id": f"p{i}",
                "prompt": long * 10,
                "topic": "t" * 100,
                "engine": "chatgpt",
                "branded": False,
                "status": "answered",
                "brands": matches,
                "citation_count": 2000,
                "error": None,
                "cost_usd": 0.004,
                "checked_at": "x",
            }
            for i in range(300)
        ],
    }
    service.ai.answer.return_value = {
        "run_id": "r",
        "prompt": long * 10,
        "answer_markdown": "a" * 100_000,
        "brands": matches,
        "citations": [
            {"url": f"https://x.com/{i}{'u' * 2000}", "title": "t" * 300, "position": i}
            for i in range(2000)
        ],
    }
    request = {"prompt": "p" * 500, "models": ["chat_gpt"], "web_search": True, "country": "US"}
    answer = {
        "model": "chat_gpt",
        "model_name": "gpt-5.6-luna",
        "status": "completed",
        "error": None,
        "text": "a" * 100_000,
        "citations": [
            {"url": f"https://x.com/{i}{'u' * 2000}", "domain": "x.com", "title": "t" * 300}
            for i in range(25)
        ],
        "fan_out_queries": ["q" * 500] * 20,
        "output_tokens": 4096,
        "web_search": True,
        "cost_usd": 0.09,
    }
    service.get_ai_lookup.return_value = {
        "lookup_id": "lookup-p",
        "kind": "prompt",
        "request": request,
        "items": [answer],
    }
    service.list_ai_lookups.return_value = [
        {"lookup_id": f"p{i}", "kind": "prompt", "request": request, "item_count": 4}
        for i in range(300)
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
        ("seo_list_ai_prompts", {}, "prompts", 100, "prompt_id"),
        ("seo_list_ai_lookups", {}, "lookups", 300, "lookup_id"),
        (
            "seo_get_ai_lookup",
            {"lookup_id": "lookup-p", "model": "chat_gpt"},
            "citations",
            25,
            "url",
        ),
        ("seo_get_ai_visibility", {}, "rows", 300, "prompt_id"),
        ("seo_get_ai_visibility", {"view": "brands"}, "brands", 101, "domain"),
        ("seo_get_ai_visibility", {"view": "sources"}, "sources", 50, None),
        (
            "seo_get_ai_answer",
            {"run_id": "r", "prompt_id": "p", "engine": "chatgpt"},
            "citations",
            2000,
            "position",
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
