"""Rank tracking: trackers, paid checks in the background, positions against an earlier date."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import octop.infra.seo.rank as rank
from octop.infra.errors import OctopError
from octop.infra.seo.rank import SeoRankService, check_cost, estimate_usd
from tests.unit.seo.test_service import configured


def serp(items, *, cost=0.002, task_status=20000):
    task = {"id": "t", "status_code": task_status, "status_message": "Ok.", "cost": cost}
    task["result"] = [{"items": items}] if task_status == 20000 else None
    return httpx.Response(200, json={"status_code": 20000, "tasks": [task]})


def organic(rank_group, domain, url=None):
    return {
        "type": "organic",
        "rank_group": rank_group,
        "rank_absolute": rank_group + 2,
        "domain": domain,
        "url": url or f"https://{domain}/",
    }


async def drained():
    await asyncio.gather(*list(rank._CHECKS))


def check(key, **extra):
    return {"idempotency_key": key, "confirm_paid": True, "max_cost_usd": 1} | extra


async def setup(context, monkeypatch, handler):
    project_id = await configured(context, monkeypatch, handler)
    return SeoRankService(context[0]), project_id


def test_estimate_counts_pages_devices_and_search_operators():
    assert check_cost("short drama", 10) == pytest.approx(0.002)
    assert check_cost("short drama", 100) == pytest.approx(0.002 + 9 * 0.0015)
    assert check_cost("SITE:example.com drama", 10) == pytest.approx(0.01)
    assert estimate_usd(["a", "b"], "both", 20) == pytest.approx(4 * 0.0035)
    assert estimate_usd(["a"], "mobile", 20) == pytest.approx(0.0035)


async def test_trackers_keywords_and_settings(context, monkeypatch):
    service, project_id = await setup(context, monkeypatch, lambda request: serp([]))
    _, _repos, user, other = context
    created = await service.create_tracker(
        user, project_id, depth=20, keywords=[" Short Drama ", "short drama", "reelshort", " "]
    )
    tracker = created["tracker"]
    tracker_id = tracker["tracker_id"]
    # The project's own domain in the project's market by default; keywords normalized once.
    assert (tracker["domain"], tracker["devices"], tracker["depth"]) == ("example.com", "both", 20)
    assert (tracker["location_code"], tracker["language_code"]) == (2840, "en")
    assert [row["keyword"] for row in created["rows"]] == ["short drama", "reelshort"]
    assert tracker["keyword_count"] == 2 and tracker["estimate_usd"] == pytest.approx(4 * 0.0035)
    assert created["run"] is None and tracker["last_checked_at"] is None
    assert created["rows"][0]["desktop"] == {
        "position": None,
        "url": None,
        "serp_features": [],
        "checked_at": None,
        "previous_position": None,
        "previous_checked_at": None,
    }
    for bad in ({"depth": 25}, {"depth": 20, "domain": "not a domain"}, {"devices": "both"}):
        with pytest.raises(OctopError) as invalid:
            await service.create_tracker(user, project_id, **bad)
        assert invalid.value.code == "SEO_INVALID_INPUT"
    with pytest.raises(OctopError) as exists:
        await service.create_tracker(user, project_id, depth=10, domain="https://www.Example.com/")
    assert exists.value.code == "SEO_CONFLICT"
    rival = await service.create_tracker(user, project_id, depth=10, domain="rival.com")
    listed = await service.list_trackers(user, project_id)
    assert [(row["domain"], row["keyword_count"]) for row in listed] == [
        ("example.com", 2),
        ("rival.com", 0),
    ]
    assert await service.add_keywords(
        user, project_id, tracker_id, keywords=["REELSHORT", "drama box"]
    ) == {"added": 1}
    monkeypatch.setattr(rank, "MAX_KEYWORDS", 4)
    with pytest.raises(OctopError) as full:
        await service.add_keywords(user, project_id, tracker_id, keywords=["x", "y"])
    assert full.value.code == "SEO_INVALID_INPUT"
    updated = await service.update_tracker(user, project_id, tracker_id, devices="mobile")
    assert (updated["tracker"]["devices"], updated["tracker"]["depth"]) == ("mobile", 20)
    assert updated["tracker"]["estimate_usd"] == pytest.approx(3 * 0.0035)
    keyword_id = updated["rows"][0]["keyword_id"]
    await service.remove_keyword(user, project_id, tracker_id, keyword_id)
    with pytest.raises(OctopError) as gone:
        await service.remove_keyword(user, project_id, tracker_id, keyword_id)
    assert gone.value.code == "NOT_FOUND"
    await service.delete_tracker(user, project_id, rival["tracker"]["tracker_id"])
    assert len(await service.list_trackers(user, project_id)) == 1
    with pytest.raises(OctopError) as forbidden:
        await service.list_trackers(other, project_id)
    assert forbidden.value.code == "FORBIDDEN"
    monkeypatch.setattr(rank, "MAX_TRACKERS", 1)
    with pytest.raises(OctopError) as capped:
        await service.create_tracker(user, project_id, depth=10, domain="third.com")
    assert capped.value.code == "SEO_INVALID_INPUT"


async def test_a_check_buys_one_serp_per_keyword_and_device(context, monkeypatch):
    bodies = []

    def handler(request):
        body = json.loads(request.content)[0]
        bodies.append(body)
        if body["keyword"] == "short drama" and body["device"] == "desktop":
            return serp(
                [
                    {"type": "ai_overview", "rank_group": 1},
                    organic(1, "rival.com"),
                    # A subdomain counts; rank_group is the place among organic results.
                    organic(2, "blog.example.com", "https://blog.example.com/top"),
                    organic(3, "example.com"),
                ],
                cost=0.002,
            )
        if body["keyword"] == "short drama":
            return serp([organic(1, "notexample.com"), {"type": "people_also_ask"}])
        return serp(None, cost=0.002, task_status=20000)

    service, project_id = await setup(context, monkeypatch, handler)
    user = context[2]
    created = await service.create_tracker(
        user, project_id, depth=20, keywords=["short drama", "reelshort"]
    )
    tracker_id = created["tracker"]["tracker_id"]
    started = await service.start_check(
        user, project_id, tracker_id, **check("k1", max_cost_usd=created["tracker"]["estimate_usd"])
    )
    assert (started["status"], started["checks_total"], started["checks_done"]) == ("running", 4, 0)
    await drained()
    assert {(b["keyword"], b["device"]) for b in bodies} == {
        ("short drama", "desktop"),
        ("short drama", "mobile"),
        ("reelshort", "desktop"),
        ("reelshort", "mobile"),
    }
    assert bodies[0] | {"keyword": "", "device": "", "os": ""} == {
        "keyword": "",
        "device": "",
        "os": "",
        "location_code": 2840,
        "language_code": "en",
        "depth": 20,
        "stop_crawl_on_match": [{"match_value": "example.com", "match_type": "with_subdomains"}],
        "find_targets_in": ["organic"],
    }
    found = await service.results(user, project_id, tracker_id)
    run = found["run"]
    assert (run["status"], run["checks_done"], run["checks_failed"]) == ("completed", 4, 0)
    assert run["cost_usd"] == pytest.approx(0.008) and run["error"] is None
    rows = {row["keyword"]: row for row in found["rows"]}
    desktop = rows["short drama"]["desktop"]
    assert (desktop["position"], desktop["url"]) == (2, "https://blog.example.com/top")
    assert desktop["serp_features"] == ["ai_overview", "organic"]
    # A single check is its own baseline, as in OpenSEO.
    assert desktop["previous_position"] == 2
    mobile = rows["short drama"]["mobile"]
    assert mobile["position"] is None and mobile["checked_at"] is not None
    assert rows["reelshort"]["desktop"]["serp_features"] == []
    assert found["tracker"]["last_checked_at"] is not None
    # The same key is the same run, and nothing more is bought.
    again = await service.start_check(user, project_id, tracker_id, **check("k1"))
    assert again["run_id"] == started["run_id"] and len(bodies) == 4
    # An estimate above what was approved buys nothing.
    with pytest.raises(OctopError) as dearer:
        await service.start_check(user, project_id, tracker_id, **check("k2", max_cost_usd=0.01))
    assert dearer.value.code == "SEO_CONFLICT" and len(bodies) == 4
    history = await service.history(user, project_id, tracker_id, rows["short drama"]["keyword_id"])
    assert sorted((s["device"], s["position"] or 0) for s in history) == [
        ("desktop", 2),
        ("mobile", 0),
    ]


async def test_previous_position_follows_the_compare_period(context, monkeypatch):
    service, project_id = await setup(context, monkeypatch, lambda request: serp([]))
    _, repos, user, _ = context
    created = await service.create_tracker(
        user, project_id, depth=10, devices="desktop", keywords=["short drama"]
    )
    tracker_id = created["tracker"]["tracker_id"]
    keyword_id = created["rows"][0]["keyword_id"]
    now = datetime.now(UTC)
    for index, (days, position) in enumerate(((20, 9), (10, 7), (3, 5), (0, 2))):
        run = {
            "run_id": f"r{index}",
            "tracker_id": tracker_id,
            "idempotency_key": f"k{index}",
            "status": "running",
            "started_at": now.isoformat(),
            "heartbeat_at": int(now.timestamp()),
        }
        assert repos.seo_rank_repo.start_run(run, 0, now.isoformat())[0] == "created"
        run["status"] = "completed"
        repos.seo_rank_repo.save_run(
            run,
            {
                "run_id": run["run_id"],
                "keyword_id": keyword_id,
                "keyword": "short drama",
                "device": "desktop",
                "position": position,
                "url": None,
                "serp_features": [],
                "cost_usd": 0.002,
                "checked_at": (now - timedelta(days=days)).isoformat(),
            },
        )

    async def previous(compare):
        found = await service.results(user, project_id, tracker_id, compare=compare)
        return found["rows"][0]["desktop"]["position"], found["rows"][0]["desktop"][
            "previous_position"
        ]

    assert await previous("1d") == (2, 5)
    assert await previous("7d") == (2, 7)
    assert await previous("30d") == (2, 9)
    # Nothing older than 90 days: the first snapshot is the baseline.
    assert await previous("90d") == (2, 9)
    with pytest.raises(OctopError):
        await service.results(user, project_id, tracker_id, compare="2d")
    # A removed keyword leaves the table but keeps its history.
    await service.remove_keyword(user, project_id, tracker_id, keyword_id)
    assert (await service.results(user, project_id, tracker_id))["rows"] == []
    history = await service.history(user, project_id, tracker_id, keyword_id)
    assert [s["position"] for s in history] == [9, 7, 5, 2]


async def test_failures_busy_trackers_and_interrupted_runs(context, monkeypatch):
    gate = asyncio.Event()
    replies = []

    async def handler(request):
        await gate.wait()
        if replies:
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        return serp([organic(4, "example.com")])

    service, project_id = await setup(context, monkeypatch, handler)
    _, repos, user, _ = context
    created = await service.create_tracker(
        user, project_id, depth=10, devices="desktop", keywords=["a", "b", "c"]
    )
    tracker_id = created["tracker"]["tracker_id"]
    replies.extend([serp(None, cost=0, task_status=40501), httpx.ConnectError("down")])
    running = await service.start_check(user, project_id, tracker_id, **check("k1"))
    # One check at a time per tracker, and a tracker being checked cannot be deleted.
    with pytest.raises(OctopError) as busy:
        await service.start_check(user, project_id, tracker_id, **check("k2"))
    assert busy.value.code == "SEO_CONFLICT"
    with pytest.raises(OctopError) as held:
        await service.delete_tracker(user, project_id, tracker_id)
    assert held.value.code == "SEO_CONFLICT"
    gate.set()
    await drained()
    run = (await service.results(user, project_id, tracker_id))["run"]
    assert run["run_id"] == running["run_id"]
    # A rejected check and a lost reply count as failed; the lost one leaves the cost unknown.
    assert (run["status"], run["checks_done"], run["checks_failed"]) == ("completed", 3, 2)
    assert (run["cost_usd"], run["cost_known_usd"]) == (None, pytest.approx(0.002))
    assert run["error"] in {"provider_rejected", "transport_unknown"}
    # Every check failing fails the run.
    replies.extend([serp(None, cost=0, task_status=40501)] * 3)
    await service.start_check(user, project_id, tracker_id, **check("k3"))
    await drained()
    run = (await service.results(user, project_id, tracker_id))["run"]
    assert (run["status"], run["checks_failed"], run["cost_usd"]) == ("failed", 3, 0)
    # A run whose heartbeat stopped (a restart) is settled as interrupted and frees the tracker.
    stale = {
        "run_id": "stale",
        "tracker_id": tracker_id,
        "idempotency_key": "old",
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "heartbeat_at": 0,
        "cost_usd": 0.004,
    }
    repos.seo_rank_repo.start_run(stale, 0, "")
    run = (await service.results(user, project_id, tracker_id))["run"]
    assert (run["run_id"], run["status"], run["error"], run["cost_usd"]) == (
        "stale",
        "unknown",
        "interrupted",
        None,
    )
    # Cancelling a running check (shutdown) closes it as interrupted with an unknown cost.
    gate.clear()
    await service.start_check(user, project_id, tracker_id, **check("k4"))
    await asyncio.sleep(0.05)
    for task in list(rank._CHECKS):
        task.cancel()
    await asyncio.gather(*list(rank._CHECKS), return_exceptions=True)
    run = (await service.results(user, project_id, tracker_id))["run"]
    assert (run["status"], run["error"], run["cost_usd"]) == ("unknown", "interrupted", None)
    await service.delete_tracker(user, project_id, tracker_id)
    assert await service.list_trackers(user, project_id) == []


async def test_a_check_needs_keywords_a_provider_and_confirmation(context, monkeypatch):
    calls = []
    service, project_id = await setup(
        context, monkeypatch, lambda request: calls.append(1) or serp([])
    )
    _, repos, user, other = context
    created = await service.create_tracker(user, project_id, depth=10)
    tracker_id = created["tracker"]["tracker_id"]
    with pytest.raises(OctopError) as empty:
        await service.start_check(user, project_id, tracker_id, **check("a"))
    assert empty.value.code == "SEO_INVALID_INPUT"
    await service.add_keywords(user, project_id, tracker_id, keywords=["x"])
    with pytest.raises(OctopError):
        await service.start_check(user, project_id, tracker_id, **check("b", confirm_paid=False))
    with pytest.raises(OctopError) as forbidden:
        await service.start_check(other, project_id, tracker_id, **check("c"))
    assert forbidden.value.code == "FORBIDDEN"
    with pytest.raises(OctopError) as missing:
        await service.results(user, project_id, "nope")
    assert missing.value.code == "NOT_FOUND"
    repos.settings_repo.delete(f"seo.provider.{user}")
    with pytest.raises(OctopError) as provider:
        await service.start_check(user, project_id, tracker_id, **check("d"))
    assert provider.value.code == "SEO_PROVIDER_REQUIRED"
    assert calls == []
