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
    # Without the seo permission an account is refused before any project lookup.
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


async def test_search_console_over_http(env_with_agent, monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    import octop.api.routers.seo_google as router_module
    from octop.infra.seo.google import oauth
    from octop.infra.seo.google.service import SeoGoogleService
    from tests.unit.seo.google.fakegoogle import SITE, FakeGoogle

    client, _server, auth, agent_id = env_with_agent
    monkeypatch.setenv(oauth.CLIENT_ID_ENV, "client-1")
    monkeypatch.setenv(oauth.CLIENT_SECRET_ENV, "secret-1")
    status = (await client.get("/api/seo/google", headers=auth)).json()
    assert status["configured"] and status["accounts"] == []
    fake = FakeGoogle(base=status["redirect_uri"].removesuffix(oauth.CALLBACK_PATH))
    monkeypatch.setattr(
        router_module,
        "SeoGoogleService",
        functools.partial(SeoGoogleService, transport=httpx.MockTransport(fake.handler)),
    )
    other = await create_user(client, auth, username="gsc-other")
    refused = await client.post("/api/seo/google/connect", headers=other, json={})
    assert refused.status_code == 403

    started = await client.post(
        "/api/seo/google/connect",
        headers=auth,
        json={"return_path": "/workbench/seo-workbench.main"},
    )
    assert started.status_code == 200, started.text
    query = parse_qs(urlsplit(started.json()["authorize_url"]).query)
    fake.verifiers["code-1"] = query["code_challenge"][0]
    # Google's redirect carries no JWT.
    back = await client.get(
        "/api/seo/google/callback", params={"state": query["state"][0], "code": "code-1"}
    )
    assert back.status_code == 303, back.text
    assert back.headers["location"] == "/workbench/seo-workbench.main?seo_google=connected"
    accounts = (await client.get("/api/seo/google", headers=auth)).json()["accounts"]
    assert [a["email"] for a in accounts] == ["owner@example.com"]
    grant_id = accounts[0]["grant_id"]

    created = await client.post(
        "/api/seo/projects",
        headers=auth,
        json={"name": "Example", "domain": "example.com", "agent_id": agent_id},
    )
    base = f"/api/seo/projects/{created.json()['project_id']}/gsc"
    assert (await client.get(base, headers=auth)).json() == {"connection": None}
    sites = (await client.get(f"{base}/sites", headers=auth)).json()
    assert sites["accounts"][0]["sites"][0] == {"site_url": SITE, "permission_level": "siteOwner"}
    not_bound = await client.post(f"{base}/report", headers=auth, json={})
    assert not_bound.status_code == 409
    assert not_bound.json()["error"]["code"] == "SEO_GOOGLE_NOT_CONNECTED"
    bound = await client.put(base, headers=auth, json={"grant_id": grant_id, "site_url": SITE})
    assert bound.status_code == 200, bound.text
    assert bound.json()["email"] == "owner@example.com"

    fake.rows = {
        ("date",): [
            {"keys": ["2027-01-01"], "clicks": 3, "impressions": 30, "ctr": 0.1, "position": 6}
        ],
        ("query", "page"): [
            {
                "keys": ["kw", "https://example.com/a"],
                "clicks": 1,
                "impressions": 20,
                "ctr": 0.05,
                "position": 9,
            }
        ],
        ("country",): [
            {"keys": ["usa"], "clicks": 3, "impressions": 30, "ctr": 0.1, "position": 6}
        ],
        ("page",): [
            {
                "keys": ["https://example.com/a"],
                "clicks": 3,
                "impressions": 30,
                "ctr": 0.1,
                "position": 6,
            }
        ],
    }
    report = await client.post(
        f"{base}/report", headers=auth, json={"date_range": "last_7_days", "device": "MOBILE"}
    )
    assert report.status_code == 200, report.text
    assert report.json()["striking_distance"][0]["query"] == "kw"
    assert report.json()["totals"]["clicks"] == 3
    bad = await client.post(f"{base}/report", headers=auth, json={"device": "WATCH"})
    assert bad.status_code == 422
    table = await client.post(f"{base}/table", headers=auth, json={"dimension": "page"})
    assert table.status_code == 200, table.text
    assert (table.json()["total_count"], table.json()["rows"][0]["key"]) == (
        1,
        "https://example.com/a",
    )
    inspected = await client.post(
        f"{base}/inspect", headers=auth, json={"urls": ["https://example.com/a"]}
    )
    assert inspected.status_code == 200, inspected.text
    assert inspected.json()["results"][0]["result"]["indexStatusResult"]["verdict"] == "PASS"

    assert (await client.delete(base, headers=auth)).status_code == 204
    assert (await client.get(base, headers=auth)).json() == {"connection": None}
    gone = await client.delete(f"/api/seo/google/accounts/{grant_id}", headers=auth)
    assert gone.status_code == 204
    assert (await client.get("/api/seo/google", headers=auth)).json()["accounts"] == []


async def test_analytics_over_http(env_with_agent, monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    import octop.api.routers.seo_google as router_module
    from octop.infra.seo.google import ga4, oauth
    from octop.infra.seo.google.service import SeoGoogleService
    from tests.unit.seo.google.fakegoogle import PROPERTY, FakeGoogle

    client, _server, auth, agent_id = env_with_agent
    monkeypatch.setenv(oauth.CLIENT_ID_ENV, "client-1")
    monkeypatch.setenv(oauth.CLIENT_SECRET_ENV, "secret-1")
    status = (await client.get("/api/seo/google", headers=auth)).json()
    fake = FakeGoogle(base=status["redirect_uri"].removesuffix(oauth.CALLBACK_PATH))
    monkeypatch.setattr(
        router_module,
        "SeoGoogleService",
        functools.partial(SeoGoogleService, transport=httpx.MockTransport(fake.handler)),
    )
    started = await client.post("/api/seo/google/connect", headers=auth, json={})
    query = parse_qs(urlsplit(started.json()["authorize_url"]).query)
    fake.verifiers["code-1"] = query["code_challenge"][0]
    back = await client.get(
        "/api/seo/google/callback", params={"state": query["state"][0], "code": "code-1"}
    )
    assert back.headers["location"] == "/workbench?seo_google=connected"
    grant_id = (await client.get("/api/seo/google", headers=auth)).json()["accounts"][0]["grant_id"]
    created = await client.post(
        "/api/seo/projects",
        headers=auth,
        json={"name": "Example", "domain": "example.com", "agent_id": agent_id},
    )
    base = f"/api/seo/projects/{created.json()['project_id']}/ga4"
    properties = (await client.get(f"{base}/properties", headers=auth)).json()
    assert properties["accounts"][0]["properties"][0]["property_id"] == PROPERTY
    bad = await client.put(base, headers=auth, json={"grant_id": grant_id, "property_id": "123"})
    assert bad.status_code == 422
    bound = await client.put(
        base, headers=auth, json={"grant_id": grant_id, "property_id": PROPERTY}
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["time_zone"] == "America/New_York"

    headers = {"metricHeaders": [{"name": m} for m in ga4.OVERVIEW_METRICS]}
    values = [{"value": v} for v in ("100", "80", "70", "0.7", "10", "4", "500")]
    fake.reports = {
        (): headers
        | {"dimensionHeaders": [], "rows": [{"dimensionValues": [], "metricValues": values}]},
        ("date",): headers
        | {
            "dimensionHeaders": [{"name": "date"}],
            "rows": [{"dimensionValues": [{"value": "20261018"}], "metricValues": values}],
        },
    }
    overview = await client.post(f"{base}/overview", headers=auth, json={})
    assert overview.status_code == 200, overview.text
    data = overview.json()
    assert data["comparison"]["sessions"]["percent_change"] == 0
    assert data["trend"][0]["date"] == "20261018"
    report = await client.post(f"{base}/report", headers=auth, json={"kind": "site_search"})
    assert report.status_code == 200, report.text
    assert report.json()["request"]["channel"] == "all"
    assert report.json()["rows"] == []
    wrong = await client.post(f"{base}/report", headers=auth, json={"kind": "funnels"})
    assert wrong.status_code == 422
    assert (await client.delete(base, headers=auth)).status_code == 204
    assert (await client.get(base, headers=auth)).json() == {"connection": None}


async def test_competitor_lookups_over_http(env_with_agent, monkeypatch):
    import octop.infra.seo.service as module

    client, server, auth, agent_id = env_with_agent
    calls = []

    def supplier(request):
        calls.append(request.url.path)
        if "domain_rank_overview" in request.url.path:
            result = {"items": [{"metrics": {"organic": {"etv": 10.5, "count": 3}}}]}
        elif "ranked_keywords" in request.url.path:
            row = {
                "keyword_data": {"keyword": "rival tips", "keyword_info": {"search_volume": 70}},
                "ranked_serp_element": {"serp_item": {"rank_group": 1, "rank_absolute": 2}},
            }
            result = {"total_count": 3, "items": [row]}
        elif request.url.path.startswith("/v3/backlinks/summary"):
            result = {"rank": 41, "backlinks": 812, "referring_domains": 51}
        elif request.url.path.startswith("/v3/backlinks/history"):
            result = {"items": [{"date": "2026-09-01 00:00:00 +00:00", "backlinks": 800}]}
        elif request.url.path.startswith("/v3/backlinks/backlinks"):
            link = {"domain_from": "a.org", "url_from": "https://a.org/x", "url_to": None}
            result = {"items": [link]}
        elif request.url.path.startswith("/v3/backlinks/referring_domains"):
            result = {"items": [{"domain": "a.org", "backlinks": 2}]}
        elif request.url.path.startswith("/v3/backlinks/domain_pages_summary"):
            result = {"items": [{"page": "https://rival.com/", "backlinks": 700}]}
        elif "relevant_pages" in request.url.path:
            page = {"page_address": "https://rival.com/a", "metrics": {"organic": {"etv": 9.5}}}
            result = {"total_count": 7, "items": [page]}
        else:
            result = {"items": [{"domain": "rival.com", "visibility": 0.5}]}
        task = {"status_code": 20000, "cost": 0.01, "result": [result]}
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
    base = f"/api/seo/projects/{created.json()['project_id']}"
    await client.put(
        "/api/seo/provider", headers=auth, json={"login": "fixture", "password": "secret"}
    )
    request = {
        "kind": "domain",
        "domain": "www.rival.com",
        "idempotency_key": "d",
        "confirm_paid": True,
    }
    bought = await client.post(base + "/lookups", headers=auth, json=request)
    assert bought.status_code == 200, bought.text
    domain = bought.json()
    assert (domain["domain"], domain["overview"]["organic_keywords"], domain["total_count"]) == (
        "rival.com",
        3,
        3,
    )
    assert domain["items"][0]["keyword"] == "rival tips" and "receipts" not in domain
    assert domain["cost_usd"] == 0.02 and domain["item_count"] == 1
    rivals = (
        await client.post(
            base + "/lookups",
            headers=auth,
            json={
                "kind": "competitors",
                "keywords": ["rival tips"],
                "idempotency_key": "c",
                "confirm_paid": True,
            },
        )
    ).json()
    assert rivals["items"] == [
        {
            "domain": "rival.com",
            "visibility": 0.5,
            "avg_position": None,
            "median_position": None,
            "etv": None,
            "keywords_count": None,
            "keywords_positions": {},
        }
    ]
    pages = (
        await client.post(
            base + "/lookups",
            headers=auth,
            json=request | {"kind": "pages", "idempotency_key": "p"},
        )
    ).json()
    # Page rows keep their own fields through the response model's union.
    assert pages["items"] == [
        {
            "url": "https://rival.com/a",
            "etv": 9.5,
            "keywords": None,
            "pos_1": None,
            "pos_2_3": None,
            "pos_4_10": None,
        }
    ]
    assert (pages["kind"], pages["total_count"]) == ("pages", 7)
    links = (
        await client.post(
            base + "/lookups",
            headers=auth,
            json=request | {"kind": "backlinks", "idempotency_key": "b"},
        )
    ).json()
    # Backlink totals and rows keep their own fields through the response unions.
    assert links["overview"]["backlinks"] == 812 and "organic_traffic" not in links["overview"]
    assert [row["section"] for row in links["items"]] == [
        "backlink",
        "referring_domain",
        "page",
        "history",
    ]
    assert links["items"][0]["url_from"] == "https://a.org/x"
    assert links["items"][1]["domain"] == "a.org" and links["items"][2]["backlinks"] == 700
    assert links["items"][3] == {
        "section": "history",
        "date": "2026-09-01",
        "backlinks": 800,
        "referring_domains": None,
        "rank": None,
        "new_backlinks": None,
        "lost_backlinks": None,
        "new_referring_domains": None,
        "lost_referring_domains": None,
    }
    for bad in (
        request | {"idempotency_key": "x", "confirm_paid": False},
        request | {"idempotency_key": "y", "keywords": ["a"]},
        {"kind": "competitors", "keywords": [], "idempotency_key": "z", "confirm_paid": True},
    ):
        assert (await client.post(base + "/lookups", headers=auth, json=bad)).status_code == 422
    summaries = (await client.get(base + "/lookups", headers=auth)).json()
    assert [row["kind"] for row in summaries] == ["backlinks", "pages", "competitors", "domain"]
    assert "items" not in summaries[0]
    only = (await client.get(base + "/lookups", headers=auth, params={"kind": "domain"})).json()
    assert [row["lookup_id"] for row in only] == [domain["lookup_id"]]
    assert (
        await client.get(base + "/lookups", headers=auth, params={"kind": "prompt"})
    ).status_code == 422
    read = (await client.get(base + "/lookups/" + domain["lookup_id"], headers=auth)).json()
    assert read["items"] == domain["items"]
    assert (await client.get(base + "/lookups/nope", headers=auth)).status_code == 404
    assert len(calls) == 9

    other = await create_user(client, auth, username="seo-r3-other")
    for method, path, body in [
        ("post", base + "/lookups", request),
        ("get", base + "/lookups", None),
        ("get", base + "/lookups/" + domain["lookup_id"], None),
    ]:
        kwargs = {"headers": other} | ({"json": body} if body is not None else {})
        assert (await getattr(client, method)(path, **kwargs)).status_code == 403, path

    from octop.api.app import build_app

    paths = build_app(server).openapi()["paths"]
    for path, method in [
        ("/api/seo/projects/{project_id}/lookups", "post"),
        ("/api/seo/projects/{project_id}/lookups", "get"),
        ("/api/seo/projects/{project_id}/lookups/{lookup_id}", "get"),
    ]:
        assert paths[path][method]["summary"], path


async def test_rank_tracking_over_http(env_with_agent, monkeypatch):
    import asyncio

    import octop.infra.seo.rank as rank
    import octop.infra.seo.service as module

    client, server, auth, agent_id = env_with_agent
    bodies = []

    def supplier(request):
        body = json.loads(request.content)[0]
        bodies.append(body)
        items = [{"type": "organic", "rank_group": 3, "domain": "www.example.com", "url": "u"}]
        task = {"status_code": 20000, "cost": 0.002, "result": [{"items": items}]}
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
    base = f"/api/seo/projects/{created.json()['project_id']}/rank-trackers"
    await client.put(
        "/api/seo/provider", headers=auth, json={"login": "fixture", "password": "secret"}
    )
    for bad in ({"depth": 15}, {"depth": 20, "devices": "tablet"}, {}):
        assert (await client.post(base, headers=auth, json=bad)).status_code == 422
    made = await client.post(
        base, headers=auth, json={"depth": 20, "devices": "desktop", "keywords": ["Short Drama"]}
    )
    assert made.status_code == 200, made.text
    tracker = made.json()["tracker"]
    path = f"{base}/{tracker['tracker_id']}"
    assert (tracker["domain"], tracker["estimate_usd"]) == ("example.com", 0.0035)
    taken = await client.post(base, headers=auth, json={"depth": 10})
    assert taken.status_code == 409 and taken.json()["error"]["details"]["reason"]
    added = await client.post(path + "/keywords", headers=auth, json={"keywords": ["reelshort"]})
    assert added.json() == {"added": 1}
    estimate = (await client.get(path, headers=auth)).json()["tracker"]["estimate_usd"]
    check = {"idempotency_key": "k", "confirm_paid": True, "max_cost_usd": estimate}
    low = await client.post(path + "/checks", headers=auth, json=check | {"max_cost_usd": 0.001})
    assert low.status_code == 409 and not bodies
    assert (
        await client.post(path + "/checks", headers=auth, json=check | {"confirm_paid": False})
    ).status_code == 422
    started = await client.post(path + "/checks", headers=auth, json=check)
    assert started.status_code == 202, started.text
    assert started.json()["checks_total"] == 2
    await asyncio.gather(*list(rank._CHECKS))
    results = (await client.get(path, headers=auth, params={"compare": "30d"})).json()
    assert results["run"]["status"] == "completed" and results["compare"] == "30d"
    assert [row["desktop"]["position"] for row in results["rows"]] == [3, 3]
    assert {body["keyword"] for body in bodies} == {"short drama", "reelshort"}
    keyword_id = results["rows"][0]["keyword_id"]
    history = (await client.get(f"{path}/keywords/{keyword_id}/history", headers=auth)).json()
    assert [row["position"] for row in history] == [3]
    assert (await client.get(path, headers=auth, params={"compare": "2d"})).status_code == 422
    patched = await client.patch(path, headers=auth, json={"devices": "both"})
    assert patched.json()["tracker"]["estimate_usd"] == 0.014
    removed = await client.delete(f"{path}/keywords/{keyword_id}", headers=auth)
    assert removed.status_code == 204
    assert (await client.delete(f"{path}/keywords/{keyword_id}", headers=auth)).status_code == 404
    listed = (await client.get(base, headers=auth)).json()
    assert [(row["keyword_count"], bool(row["last_checked_at"])) for row in listed] == [(1, True)]

    other = await create_user(client, auth, username="seo-rank-other")
    for method, url, body in [
        ("get", base, None),
        ("post", path + "/checks", check),
        ("get", path, None),
    ]:
        kwargs = {"headers": other} | ({"json": body} if body is not None else {})
        assert (await getattr(client, method)(url, **kwargs)).status_code == 403, url
    assert (await client.delete(path, headers=auth)).status_code == 204
    assert (await client.get(path, headers=auth)).status_code == 404

    from octop.api.app import build_app

    paths = build_app(server).openapi()["paths"]
    tracker_path = "/api/seo/projects/{project_id}/rank-trackers/{tracker_id}"
    for route, method in [
        ("/api/seo/projects/{project_id}/rank-trackers", "get"),
        ("/api/seo/projects/{project_id}/rank-trackers", "post"),
        (tracker_path, "get"),
        (tracker_path, "patch"),
        (tracker_path, "delete"),
        (tracker_path + "/keywords", "post"),
        (tracker_path + "/keywords/{keyword_id}", "delete"),
        (tracker_path + "/keywords/{keyword_id}/history", "get"),
        (tracker_path + "/checks", "post"),
    ]:
        assert paths[route][method]["summary"], route


async def test_ai_visibility_over_http(env_with_agent, monkeypatch):
    import asyncio

    import octop.infra.seo.ai_visibility as ai
    import octop.infra.seo.service as module

    client, server, auth, agent_id = env_with_agent
    paths_asked = []

    def supplier(request):
        paths_asked.append(request.url.path)
        result = {"markdown": "Example and Rival", "sources": [{"url": "https://example.com/"}]}
        task = {"status_code": 20000, "cost": 0.004, "result": [result]}
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
    base = f"/api/seo/projects/{created.json()['project_id']}/ai-visibility"
    await client.put(
        "/api/seo/provider", headers=auth, json={"login": "fixture", "password": "secret"}
    )
    empty = await client.get(base, headers=auth)
    assert empty.status_code == 200, empty.text
    assert empty.json()["settings"]["engines"] == ["chatgpt", "gemini"]
    for bad in ({"prompts": []}, {"prompts": ["x"], "topic": ""}):
        assert (await client.post(base + "/prompts", headers=auth, json=bad)).status_code == 422
    added = await client.post(
        base + "/prompts", headers=auth, json={"prompts": ["best short drama apps"]}
    )
    assert added.json() == {"added": 1}
    bad_engine = await client.put(base + "/engines", headers=auth, json={"engines": ["claude"]})
    assert bad_engine.status_code == 422
    put = await client.put(base + "/engines", headers=auth, json={"engines": ["chatgpt"]})
    estimate = put.json()["settings"]["estimate_usd"]
    assert estimate == 0.004
    check = {"idempotency_key": "k", "confirm_paid": True, "max_cost_usd": estimate}
    low = await client.post(base + "/checks", headers=auth, json=check | {"max_cost_usd": 0.001})
    assert low.status_code == 409 and low.json()["error"]["details"]["reason"]
    assert not paths_asked
    started = await client.post(base + "/checks", headers=auth, json=check)
    assert started.status_code == 202, started.text
    assert started.json()["answers_total"] == 1
    await asyncio.gather(*list(ai._CHECKS))
    results = (await client.get(base, headers=auth, params={"branded": "all"})).json()
    assert results["run"]["status"] == "completed"
    assert paths_asked == ["/v3/ai_optimization/chat_gpt/llm_scraper/live/advanced"]
    row = results["rows"][0]
    assert (row["status"], row["brands"][0]["mentioned"], row["brands"][0]["cited"]) == (
        "answered",
        True,
        True,
    )
    assert results["summaries"][0]["own"] and results["sources"][0]["owner"] == "own"
    run_id = results["run"]["run_id"]
    answer_path = f"{base}/runs/{run_id}/answers/{row['prompt_id']}/chatgpt"
    answer = (await client.get(answer_path, headers=auth)).json()
    assert answer["answer_markdown"] == "Example and Rival"
    assert answer["citations"][0]["owner"] == "own"
    assert (await client.get(base, headers=auth, params={"branded": "x"})).status_code == 422
    assert (await client.get(base, headers=auth, params={"run_id": "x"})).status_code == 404
    removed = await client.delete(f"{base}/prompts/{row['prompt_id']}", headers=auth)
    assert removed.status_code == 204
    gone = await client.delete(f"{base}/prompts/{row['prompt_id']}", headers=auth)
    assert gone.status_code == 404

    other = await create_user(client, auth, username="seo-ai-other")
    for method, url, body in [
        ("get", base, None),
        ("post", base + "/checks", check),
        ("get", answer_path, None),
    ]:
        kwargs = {"headers": other} | ({"json": body} if body is not None else {})
        assert (await getattr(client, method)(url, **kwargs)).status_code == 403, url

    from octop.api.app import build_app

    paths = build_app(server).openapi()["paths"]
    ai_path = "/api/seo/projects/{project_id}/ai-visibility"
    for route, method in [
        (ai_path, "get"),
        (ai_path + "/engines", "put"),
        (ai_path + "/prompts", "post"),
        (ai_path + "/prompts/{prompt_id}", "delete"),
        (ai_path + "/runs/{run_id}/answers/{prompt_id}/{engine}", "get"),
        (ai_path + "/checks", "post"),
    ]:
        assert paths[route][method]["summary"], route


async def test_seo_permission_shares_the_admin_workspace(env_with_agent, monkeypatch):
    import octop.infra.seo.service as module

    client, _server, auth, agent_id = env_with_agent
    calls = []

    def supplier(request):
        calls.append(json.loads(request.content))
        item = {"keyword": "example seo", "keyword_info": {"search_volume": 120}}
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
    member = await create_user(client, auth, username="seo-member", permissions=["seo"])

    # The member works in the admin's projects, on the admin's provider credentials.
    listed = (await client.get("/api/seo/projects", headers=member)).json()
    assert [p["project_id"] for p in listed] == [project_id]
    assert (await client.get("/api/seo/provider", headers=member)).json() == {"configured": True}
    request = {
        "keywords": ["example"],
        "mode": "ideas",
        "idempotency_key": "member",
        "confirm_paid": True,
    }
    researched = await client.post(base + "/research", headers=member, json=request)
    assert researched.status_code == 200, researched.text
    run_id = researched.json()["run_id"]
    assert len(calls) == 1
    assert [r["run_id"] for r in (await client.get(base + "/runs", headers=auth)).json()] == [
        run_id
    ]

    # Project setup, provider credentials and Google accounts stay with the admin.
    for method, path, body in [
        ("post", "/api/seo/projects", {"name": "X", "domain": "x.com", "agent_id": agent_id}),
        ("patch", base, {"agent_id": agent_id}),
        ("put", "/api/seo/provider", {"login": "member", "password": "secret"}),
        ("post", "/api/seo/google/connect", {}),
        ("delete", "/api/seo/google/accounts/grant-1", None),
    ]:
        kwargs = {"headers": member} | ({"json": body} if body is not None else {})
        assert (await getattr(client, method)(path, **kwargs)).status_code == 403, path
