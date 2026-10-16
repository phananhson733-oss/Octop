"""Agent deletion keeps SEO evidence, rebinding, run summaries and report background."""

import pytest

from octop.infra.errors import OctopError
from tests.unit.seo.test_service import configured, response

HTML = "<html><body>report</body></html>"


async def researched(context, monkeypatch):
    def handler(request):
        return response([{"keyword": k, "keyword_info": {"search_volume": 10}} for k in "abc"])

    project_id = await configured(context, monkeypatch, handler)
    service, repos, user, _ = context
    run = await service.research(
        user,
        project_id,
        keywords=["seed"],
        mode="ideas",
        idempotency_key="one",
        confirm_paid=True,
    )
    await service.save_keywords(user, project_id, run_id=run["run_id"], keywords=["a", "b"])
    return project_id, run


async def test_deleting_the_agent_keeps_the_project_until_rebound(context, monkeypatch):
    service, repos, user, other = context
    project_id, run = await researched(context, monkeypatch)
    evidence = await service.report_context(user, project_id, run["run_id"])
    report = await service.save_report(
        user,
        project_id,
        run_id=run["run_id"],
        context_hash=evidence["context_hash"],
        title="Before",
        summary="",
        html=HTML,
    )
    repos.agent_repo.delete("seo")
    (project,) = await service.list_projects(user)
    assert project["agent_id"] is None
    assert (await service.get_run(user, project_id, run["run_id"]))["results"][0]["ok"]
    assert len(await service.keyword_rows(user, project_id)) == 2
    assert (await service.get_report(user, project_id, report["report_id"]))["html"] == HTML
    assert await service.project_for_agent(user, "seo") is None

    repos.agent_repo.create(agent_id="seo2", user_id=user, name="SEO 2")
    project, previous = await service.bind_agent(user, project_id, agent_id="seo2")
    assert project["agent_id"] == "seo2" and previous is None
    assert (await service.project_for_agent(user, "seo2"))["project_id"] == project_id
    repos.agent_repo.create(agent_id="seo3", user_id=user, name="SEO 3")
    project, previous = await service.bind_agent(user, project_id, agent_id="seo3")
    assert previous == "seo2" and await service.project_for_agent(user, "seo2") is None

    # One expert serves one project; someone else's expert is refused.
    repos.agent_repo.create(agent_id="seo4", user_id=user, name="SEO 4")
    await service.create_project(user, name="B", domain="b.com", agent_id="seo4")
    with pytest.raises(OctopError) as taken:
        await service.bind_agent(user, project_id, agent_id="seo4")
    assert taken.value.code == "SEO_CONFLICT"
    repos.agent_repo.create(agent_id="bob-seo", user_id=other, name="Bob")
    with pytest.raises(OctopError) as foreign:
        await service.bind_agent(user, project_id, agent_id="bob-seo")
    assert foreign.value.code == "FORBIDDEN"
    with pytest.raises(OctopError):
        await service.bind_agent(other, project_id, agent_id="bob-seo")


async def test_run_listing_has_no_rows_and_one_run_loads_alone(context, monkeypatch):
    service, repos, user, _ = context
    project_id, run = await researched(context, monkeypatch)
    second = await service.research(
        user,
        project_id,
        keywords=["other"],
        mode="ideas",
        idempotency_key="two",
        confirm_paid=True,
    )
    listed = await service.list_runs(user, project_id)
    assert [r["run_id"] for r in listed] == [second["run_id"], run["run_id"]]
    assert listed[0]["results"][0]["row_count"] == 3
    assert all("rows" not in result for item in listed for result in item["results"])
    assert listed[0]["cost_usd"] == 0.01 and listed[0]["cost_known_usd"] == 0.01

    def every_run(*_args):
        raise AssertionError("loading one run must not list every run")

    monkeypatch.setattr(repos.seo_repo, "run_summaries", every_run)
    loaded = await service.get_run(user, project_id, run["run_id"])
    assert len(loaded["results"][0]["rows"]) == 3 and loaded["cost_usd"] == 0.01


async def test_report_evidence_has_background_but_not_the_research_log(context, monkeypatch):
    service, repos, user, _ = context
    project_id, run = await researched(context, monkeypatch)
    first = await service.report_context(user, project_id, run["run_id"])
    assert first["project_context"]["missing_sections"][0] == "business_overview"
    assert "research_log" not in first["project_context"]
    await service.update_context(
        user, project_id, [{"append_research_log": {"summary": "noted"}}], author="agent"
    )
    same = await service.report_context(user, project_id, run["run_id"])
    assert same["context_hash"] == first["context_hash"]
    await service.update_context(
        user, project_id, [{"add_competitors": [{"domain": "rival.com"}]}], author="user"
    )
    changed = await service.report_context(user, project_id, run["run_id"])
    assert changed["context_hash"] != first["context_hash"]
    assert changed["project_context"]["competitors"][0]["domain"] == "rival.com"
    with pytest.raises(OctopError) as stale:
        await service.save_report(
            user,
            project_id,
            run_id=run["run_id"],
            context_hash=first["context_hash"],
            title="Stale",
            summary="",
            html=HTML,
        )
    assert stale.value.code == "SEO_CONFLICT"
