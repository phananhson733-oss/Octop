"""Native SEO HTTP workflow with real temporary SQLite and a mocked supplier."""

from __future__ import annotations

import functools
import json

import httpx

from octop.infra.seo.provider import DataForSeoClient
from tests.support.auth import create_user


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
