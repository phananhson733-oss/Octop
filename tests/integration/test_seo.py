"""Native SEO HTTP workflow with real temporary SQLite and a mocked supplier."""

from __future__ import annotations

import functools
import json

import httpx

from octop.infra.seo.provider import DataForSeoClient
from tests.support.auth import create_agent, create_user


async def test_private_keyword_and_report_workflow(env_with_agent, monkeypatch):
    import octop.infra.seo.service as module

    client, server, auth, agent_id = env_with_agent
    calls = []

    def supplier(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "status_code": 20000,
                "tasks": [
                    {
                        "status_code": 20000,
                        "cost": 0.01,
                        "result": [
                            {
                                "items": [
                                    {
                                        "keyword": "example seo",
                                        "keyword_info": {"search_volume": 120, "cpc": 1.5},
                                    },
                                    {"keyword": "missing metric"},
                                ]
                            }
                        ],
                    }
                ],
            },
        )

    monkeypatch.setattr(
        module,
        "DataForSeoClient",
        functools.partial(DataForSeoClient, transport=httpx.MockTransport(supplier)),
    )
    created = await client.post(
        "/api/seo/projects",
        headers=auth,
        json={"name": "Example", "domain": "example.com", "agent_id": agent_id},
    )
    assert created.status_code == 200, created.text
    project_id = created.json()["project_id"]
    base = f"/api/seo/projects/{project_id}"
    other = await create_user(client, auth, username="seo-other")
    # SEO is admin-only: an ordinary account is refused before any project lookup.
    assert (await client.get("/api/seo/projects", headers=other)).status_code == 403
    assert (await client.get(base + "/keywords", headers=other)).status_code == 403
    assert (
        await client.post(
            "/api/seo/projects",
            headers=other,
            json={"name": "Other", "domain": "example.org", "agent_id": agent_id},
        )
    ).status_code == 403
    request = {
        "keywords": ["example"],
        "mode": "ideas",
        "idempotency_key": "http",
        "confirm_paid": True,
    }
    assert (await client.post(base + "/research", headers=auth, json=request)).status_code == 409
    assert (
        await client.put(
            "/api/seo/provider", headers=auth, json={"login": "fixture", "password": "secret"}
        )
    ).json() == {"configured": True}
    assert (await client.get("/api/seo/provider", headers=auth)).json() == {"configured": True}
    assert (await client.get("/api/seo/provider", headers=other)).status_code == 403
    assert (
        await client.post(base + "/research", headers=auth, json=request | {"confirm_paid": False})
    ).status_code == 422
    researched = await client.post(base + "/research", headers=auth, json=request)
    assert researched.status_code == 200, researched.text
    run = researched.json()
    assert run["status"] == "completed" and run["cost_usd"] == 0.01
    saved = await client.post(
        base + "/keywords",
        headers=auth,
        json={"run_id": run["run_id"], "keywords": ["example seo"], "tags": ["priority"]},
    )
    assert saved.json() == {"saved_count": 1, "created_count": 1, "updated_count": 0}
    forged = await client.post(
        base + "/keywords",
        headers=auth,
        json={
            "run_id": run["run_id"],
            "keywords": ["example seo"],
            "metrics": [{"search_volume": 999999}],
        },
    )
    assert forged.status_code == 422
    keywords = (await client.get(base + "/keywords", headers=auth)).json()
    assert keywords["rows"][0]["search_volume"] == 120
    assert keywords["rows"][0]["observed_at"] == run["observed_at"]
    context = (
        await client.get(base + "/report-context", headers=auth, params={"run_id": run["run_id"]})
    ).json()
    assert context["saved_keywords"][0]["tags"] == ["priority"]
    # This is explicitly an HTML fixture; production report generation is an Agent tool step.
    html = "<html><body>Offline evidence: example seo, volume 120</body></html>"
    report = await client.post(
        base + "/reports",
        headers=auth,
        json={
            "run_id": run["run_id"],
            "context_hash": context["context_hash"],
            "title": "Fixture",
            "summary": "Offline evidence",
            "html": html,
        },
    )
    assert report.status_code == 200, report.text
    metadata = report.json()
    assert "html" not in metadata
    listed = (await client.get(base + "/reports", headers=auth)).json()
    assert len(listed) == 1 and "html" not in listed[0]
    url = base + "/reports/" + metadata["report_id"]
    readback = await client.get(url, headers=auth)
    assert readback.json()["html"] == html
    assert readback.headers["cache-control"] == "no-store"
    assert (await client.get(url, headers=other)).status_code == 403
    assert len(calls) == 1
    # The standard test server disables public API docs; validate its generated schema.
    from octop.api.app import build_app

    schema = build_app(server).openapi()
    assert schema["paths"]["/api/seo/projects/{project_id}/reports"]["post"]["summary"]
    assert schema["paths"]["/api/seo/projects/{project_id}/reports"]["post"]["requestBody"]


async def test_serp_background_and_rebinding_over_http(env_with_agent, monkeypatch):
    import octop.infra.seo.service as module

    client, server, auth, agent_id = env_with_agent
    calls = []

    def supplier(request):
        calls.append(request.url.path)
        item = (
            {"type": "organic", "rank_group": 1, "rank_absolute": 3, "domain": "a.com"}
            if "serp" in request.url.path
            else {"keyword": "example seo", "keyword_info": {"search_volume": 120}}
        )
        task = {"status_code": 20000, "cost": 0.01, "result": [{"items": [item]}]}
        return httpx.Response(200, json={"status_code": 20000, "tasks": [task]})

    monkeypatch.setattr(
        module,
        "DataForSeoClient",
        functools.partial(DataForSeoClient, transport=httpx.MockTransport(supplier)),
    )
    created = await client.post(
        "/api/seo/projects",
        headers=auth,
        json={"name": "Example", "domain": "example.com", "agent_id": agent_id},
    )
    project_id = created.json()["project_id"]
    base = f"/api/seo/projects/{project_id}"
    await client.put(
        "/api/seo/provider", headers=auth, json={"login": "fixture", "password": "secret"}
    )
    run = (
        await client.post(
            base + "/research",
            headers=auth,
            json={
                "keywords": ["example"],
                "mode": "ideas",
                "idempotency_key": "r",
                "confirm_paid": True,
            },
        )
    ).json()
    listed = (await client.get(base + "/runs", headers=auth)).json()
    assert listed[0]["results"][0]["row_count"] == 1 and "rows" not in listed[0]["results"][0]
    one = (await client.get(base + "/runs/" + run["run_id"], headers=auth)).json()
    assert one["results"][0]["rows"][0]["keyword"] == "example seo"

    serp_request = {"keyword": "example seo", "idempotency_key": "s", "confirm_paid": True}
    bought = await client.post(base + "/serps", headers=auth, json=serp_request)
    assert bought.status_code == 200, bought.text
    serp = bought.json()
    assert (serp["item_count"], serp["items"][0]["rank"], serp["items"][0]["organic_rank"]) == (
        1,
        3,
        1,
    )
    assert "receipt" not in serp
    unpaid = serp_request | {"idempotency_key": "s2", "confirm_paid": False}
    assert (await client.post(base + "/serps", headers=auth, json=unpaid)).status_code == 422
    summaries = (
        await client.get(base + "/serps", headers=auth, params={"keyword": "example seo"})
    ).json()
    assert [s["serp_id"] for s in summaries] == [serp["serp_id"]] and "items" not in summaries[0]
    read = (await client.get(base + "/serps/" + serp["serp_id"], headers=auth)).json()
    assert read["items"] == serp["items"]
    assert calls.count("/v3/serp/google/organic/live/advanced") == 1

    patched = await client.patch(
        base + "/context",
        headers=auth,
        json={
            "updates": [
                {"section": "current_goal", "content": "Grow"},
                {"add_competitors": [{"domain": "www.rival.com"}]},
            ]
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["competitors"][0]["domain"] == "rival.com"
    assert patched.json()["sections"][0]["updated_by"] == "user"
    bad = await client.patch(
        base + "/context", headers=auth, json={"updates": [{"section": "x", "content": ""}]}
    )
    assert bad.status_code == 422
    background = (await client.get(base + "/context", headers=auth)).json()
    assert "business_overview" in background["missing_sections"]

    # Deleting the expert keeps the project; rebinding restores report generation.
    assert (await client.delete(f"/api/agents/{agent_id}", headers=auth)).status_code == 204
    (project,) = (await client.get("/api/seo/projects", headers=auth)).json()
    assert project["agent_id"] is None
    assert (await client.get(base + "/runs/" + run["run_id"], headers=auth)).status_code == 200
    second = await create_agent(client, auth, name="seo-second")
    rebound = await client.patch(base, headers=auth, json={"agent_id": second})
    assert rebound.status_code == 200, rebound.text
    assert rebound.json()["agent_id"] == second

    other = await create_user(client, auth, username="seo-r1-other")
    for method, path, body in [
        ("patch", base, {"agent_id": second}),
        ("get", base + "/runs", None),
        ("post", base + "/serps", serp_request),
        ("get", base + "/serps", None),
        ("get", base + "/serps/" + serp["serp_id"], None),
        ("get", base + "/context", None),
        ("patch", base + "/context", {"updates": [{"section": "current_goal", "content": ""}]}),
    ]:
        kwargs = {"headers": other} | ({"json": body} if body is not None else {})
        assert (await getattr(client, method)(path, **kwargs)).status_code == 403, path

    from octop.api.app import build_app

    paths = build_app(server).openapi()["paths"]
    for path, method in [
        ("/api/seo/projects/{project_id}", "patch"),
        ("/api/seo/projects/{project_id}/serps", "post"),
        ("/api/seo/projects/{project_id}/serps", "get"),
        ("/api/seo/projects/{project_id}/serps/{serp_id}", "get"),
        ("/api/seo/projects/{project_id}/context", "get"),
        ("/api/seo/projects/{project_id}/context", "patch"),
    ]:
        assert paths[path][method]["summary"], path


async def test_site_audit_over_http(env_with_agent, monkeypatch):
    from octop.infra.seo.audit import runner as runner_module
    from octop.infra.seo.audit.throttle import CrawlThrottle
    from tests.unit.seo.audit.fakesite import FakeSite, page

    client, server, auth, agent_id = env_with_agent
    clock = {"now": 0.0}

    async def no_wait(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr(
        runner_module, "CrawlThrottle", lambda: CrawlThrottle(lambda: clock["now"], no_wait)
    )
    site = FakeSite(
        {
            "https://example.com/": page("Home", "/a", "/missing"),
            "https://example.com/a": page("Ay", "/"),
        }
    )
    runner = server.app_runtime.seo_audits
    monkeypatch.setattr(runner, "_client_factory", site.client)

    created = await client.post(
        "/api/seo/projects",
        headers=auth,
        json={"name": "Example", "domain": "example.com", "agent_id": agent_id},
    )
    base = f"/api/seo/projects/{created.json()['project_id']}/audits"
    other = await create_user(client, auth, username="audit-other")
    assert (await client.get(base, headers=other)).status_code == 403
    bad = await client.post(
        base,
        headers=auth | {"Accept-Language": "en"},
        json={"idempotency_key": "bad", "start_url": "http://10.0.0.1/"},
    )
    assert bad.status_code == 400
    assert "private or internal network" in bad.json()["error"]["details"]["reason"]

    started = await client.post(base, headers=auth, json={"idempotency_key": "one"})
    assert started.status_code == 200, started.text
    audit_id = started.json()["audit_id"]
    assert started.json()["max_pages"] == 50
    await runner.wait(audit_id)

    listed = (await client.get(base, headers=auth)).json()
    assert [row["audit_id"] for row in listed] == [audit_id]
    detail = (await client.get(f"{base}/{audit_id}", headers=auth)).json()
    assert (detail["status"], detail["pages_crawled"]) == ("completed", 3)
    assert detail["issue_counts"]["critical"] == 1
    issues = (
        await client.get(
            f"{base}/{audit_id}/issues",
            headers=auth | {"Accept-Language": "zh-CN"},
            params={"severity": "critical"},
        )
    ).json()
    assert [row["issue_type"] for row in issues["issues"]] == ["broken-internal-link"]
    assert issues["summary"][0]["title"] == "站内死链"
    pages = (
        await client.get(f"{base}/{audit_id}/pages", headers=auth, params={"status_code": 404})
    ).json()["pages"]
    assert [row["url"] for row in pages] == ["https://example.com/missing"]

    queued = await client.post(base, headers=auth, json={"idempotency_key": "two"})
    second = queued.json()["audit_id"]
    cancelled = await client.post(f"{base}/{second}/cancel", headers=auth)
    assert cancelled.status_code == 200
    await runner.wait(second)
    assert (await client.get(f"{base}/{second}", headers=auth)).json()["status"] == "cancelled"
    assert (await client.delete(f"{base}/{audit_id}", headers=auth)).status_code == 204
    assert (await client.get(f"{base}/{audit_id}", headers=auth)).status_code == 404
