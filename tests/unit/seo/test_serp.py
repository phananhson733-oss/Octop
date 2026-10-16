"""SERP snapshots: paid confirmation, idempotency and OpenSEO's cache-depth rules."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from octop.infra.errors import OctopError
from tests.unit.seo.test_service import configured

ITEMS = [
    {"type": "people_also_ask", "rank_group": 1, "rank_absolute": 1},
    {
        "type": "organic",
        "rank_group": 1,
        "rank_absolute": 2,
        "domain": "a.com",
        "url": "https://a.com/",
        "title": "A",
        "backlinks_info": {"referring_domains": 7, "backlinks": 9},
    },
    {"type": "organic", "rank_group": 2, "domain": "b.com"},
]


def serp_response(items, *, cost=0.003, task_status=20000, message="Ok."):
    task = {"id": "task", "status_code": task_status, "status_message": message, "cost": cost}
    task["result"] = [{"items": items}] if task_status == 20000 else None
    return httpx.Response(200, json={"status_code": 20000, "tasks": [task]})


def paid(key, **extra):
    return {"keyword": "crm", "idempotency_key": key, "confirm_paid": True} | extra


async def test_serp_maps_both_ranks_and_buys_once_per_key(context, monkeypatch):
    bodies = []

    def handler(request):
        bodies.append((request.url.path, json.loads(request.content)[0]))
        return serp_response(ITEMS)

    project_id = await configured(context, monkeypatch, handler)
    service, repos, user, _ = context
    command = paid("k1", keyword=" Best CRM ", device="mobile")
    serp = await service.serp(user, project_id, **command)
    assert bodies == [
        (
            "/v3/serp/google/organic/live/advanced",
            {
                "keyword": "best crm",
                "location_code": 2840,
                "language_code": "en",
                "device": "mobile",
                "os": "android",
                "depth": 20,
            },
        )
    ]
    assert serp["status"] == "completed" and serp["cost_usd"] == 0.003 and not serp["cached"]
    # rank counts every element; organic_rank only organic results.
    assert [(i["type"], i["rank"], i["organic_rank"]) for i in serp["items"]] == [
        ("people_also_ask", 1, None),
        ("organic", 2, 1),
        ("organic", 2, 2),
    ]
    assert serp["items"][1]["referring_domains"] == 7
    again = await service.serp(user, project_id, **command)
    assert again["serp_id"] == serp["serp_id"] and len(bodies) == 1
    with pytest.raises(OctopError) as conflict:
        await service.serp(user, project_id, **(command | {"depth": 100}))
    assert conflict.value.code == "SEO_CONFLICT"
    for bad in (paid("k2", depth=50), paid("k3", confirm_paid=False), paid("k4", keyword=" ")):
        with pytest.raises(OctopError):
            await service.serp(user, project_id, **bad)
    assert len(bodies) == 1
    listed = await service.list_serps(user, project_id, keyword="BEST crm")
    assert [s["serp_id"] for s in listed] == [serp["serp_id"]]
    assert listed[0]["item_count"] == 3 and "items" not in listed[0]
    assert await service.list_serps(user, project_id, keyword="other") == []
    assert (await service.get_serp(user, project_id, serp["serp_id"]))["items"] == serp["items"]


async def test_serp_cache_follows_openseo_depth_rules(context, monkeypatch):
    depths = []
    replies = []

    def handler(request):
        depths.append(json.loads(request.content)[0]["depth"])
        return replies.pop(0)

    project_id = await configured(context, monkeypatch, handler)
    service, repos, user, _ = context
    replies.append(serp_response(ITEMS))
    shallow = await service.serp(user, project_id, **paid("a"))
    # An empty deeper crawl is returned but never evicts the good shallow snapshot.
    replies.append(serp_response([]))
    empty = await service.serp(user, project_id, **paid("b", depth=100))
    assert empty["items"] == [] and depths == [20, 100]
    reused = await service.serp(user, project_id, **paid("c"))
    assert reused["cached"] and reused["source_serp_id"] == shallow["serp_id"]
    assert reused["cost_usd"] == 0 and reused["observed_at"] == shallow["observed_at"]
    # A 20-deep snapshot cannot answer a 100-deep request.
    replies.append(serp_response(ITEMS * 3))
    deep = await service.serp(user, project_id, **paid("d", depth=100))
    assert depths == [20, 100, 100]
    # A 100-deep snapshot answers a 20-deep one, at its own depth.
    reused = await service.serp(user, project_id, **paid("e"))
    assert reused["source_serp_id"] == deep["serp_id"]
    assert (reused["depth"], reused["requested_depth"]) == (100, 20)
    # Another device is another market.
    replies.append(serp_response(ITEMS))
    await service.serp(user, project_id, **paid("f", device="mobile"))
    assert len(depths) == 4
    # After 12 hours the snapshot is bought again.
    with repos.seo_repo._db.transaction() as conn:
        conn.execute("UPDATE seo_serps SET created_at=created_at-43201")
    replies.append(serp_response(ITEMS))
    fresh = await service.serp(user, project_id, **paid("g"))
    assert not fresh["cached"] and len(depths) == 5


async def test_serp_no_results_failures_and_interruptions_keep_evidence(context, monkeypatch):
    replies = [
        serp_response([], task_status=40501, message="No Search Results.", cost=0.002),
        serp_response([], task_status=40501, message="Invalid Field: 'keyword'.", cost=0),
    ]

    def handler(request):
        if not replies:
            raise httpx.ConnectError("down")
        return replies.pop(0)

    project_id = await configured(context, monkeypatch, handler)
    service, repos, user, _ = context
    empty = await service.serp(user, project_id, **paid("a"))
    assert (empty["status"], empty["items"], empty["cost_usd"]) == ("completed", [], 0.002)
    failed = await service.serp(user, project_id, **paid("b"))
    assert (failed["status"], failed["cost_usd"], failed["error"]) == (
        "failed",
        0,
        "provider_rejected",
    )
    unknown = await service.serp(user, project_id, **paid("c"))
    assert (unknown["status"], unknown["cost_usd"]) == ("unknown", None)
    # A row still running after 5 minutes was interrupted, and is never bought again.
    unknown.update(
        status="running", observed_at=(datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    )
    repos.seo_repo.update_serp(unknown)
    listed = await service.list_serps(user, project_id)
    assert listed[0]["status"] == "unknown" and listed[0]["error"] == "interrupted"
    replay = await service.serp(user, project_id, **paid("c"))
    assert replay["status"] == "unknown"
    with pytest.raises(OctopError) as missing:
        await service.get_serp(user, project_id, "nope")
    assert missing.value.code == "NOT_FOUND"
