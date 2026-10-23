"""Competitor lookups: Labs mapping, paid confirmation, idempotency and 12-hour reuse."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from octop.infra.errors import OctopError
from tests.unit.seo.test_service import configured

OVERVIEW = [{"metrics": {"organic": {"etv": 1234.5, "count": 87, "pos_1": 3, "pos_2_3": 5}}}]
RANKED = [
    {
        "keyword_data": {
            "keyword": "Short Drama",
            "keyword_info": {"search_volume": 900, "cpc": 0.4},
            "keyword_properties": {"keyword_difficulty": 31},
            "search_intent_info": {"main_intent": "informational"},
        },
        "ranked_serp_element": {
            "serp_item": {
                "type": "organic",
                "rank_group": 2,
                "rank_absolute": 4,
                "url": "https://rival.com/a",
                "etv": 55.2,
            }
        },
    },
    {"keyword_data": {}},
]
COMPETITORS = [
    {"domain": "low.com", "visibility": 0.1, "avg_position": 9},
    {
        "domain": "rival.com",
        "visibility": 0.7,
        "avg_position": 3.5,
        "median_position": 3,
        "etv": 120,
        "keywords_count": 2,
        "keywords_positions": {"short drama": [4]},
    },
    {"visibility": 0.9},
]


def labs(result, *, cost=0.0101, task_status=20000):
    task = {"id": "t", "status_code": task_status, "status_message": "Ok.", "cost": cost}
    task["result"] = [result] if task_status == 20000 else None
    return httpx.Response(200, json={"status_code": 20000, "tasks": [task]})


def domain(key, **extra):
    return (
        {"kind": "domain", "domain": "https://www.Rival.com/x", "idempotency_key": key}
        | {"confirm_paid": True}
        | extra
    )


def competitors(key, keywords, **extra):
    return {
        "kind": "competitors",
        "keywords": keywords,
        "idempotency_key": key,
        "confirm_paid": True,
    } | extra


async def test_domain_lookup_buys_overview_and_ranked_keywords_once(context, monkeypatch):
    calls = []

    def handler(request):
        path, body = request.url.path, json.loads(request.content)[0]
        calls.append((path, body))
        if path.endswith("domain_rank_overview/live"):
            return labs({"items": OVERVIEW})
        return labs({"total_count": 87, "items": RANKED}, cost=0.02)

    project_id = await configured(context, monkeypatch, handler)
    service, _repos, user, _ = context
    found = await service.lookup(user, project_id, **domain("k1"))
    assert [path for path, _ in calls] == [
        "/v3/dataforseo_labs/google/domain_rank_overview/live",
        "/v3/dataforseo_labs/google/ranked_keywords/live",
    ]
    market = {"target": "rival.com", "location_code": 2840, "language_code": "en"}
    assert calls[0][1] == market | {"limit": 1}
    assert calls[1][1] == market | {
        "limit": 100,
        "order_by": ["ranked_serp_element.serp_item.etv,desc"],
        "item_types": ["organic"],
    }
    assert (found["status"], found["domain"], found["keywords"]) == ("completed", "rival.com", None)
    assert found["overview"] == {
        "organic_traffic": 1234.5,
        "organic_keywords": 87,
        "pos_1": 3,
        "pos_2_3": 5,
        "pos_4_10": None,
    }
    assert found["total_count"] == 87 and found["cost_usd"] == pytest.approx(0.0301)
    # Keyword rows are normalized; a row without a keyword is dropped.
    assert found["items"] == [
        {
            "keyword": "short drama",
            "rank": 4,
            "organic_rank": 2,
            "url": "https://rival.com/a",
            "etv": 55.2,
            "search_volume": 900,
            "cpc": 0.4,
            "keyword_difficulty": 31,
            "intent": "informational",
        }
    ]
    assert len(found["receipts"]) == 2
    again = await service.lookup(user, project_id, **domain("k1"))
    assert again["lookup_id"] == found["lookup_id"] and len(calls) == 2
    # The same key for another target is a conflict, never a second purchase.
    with pytest.raises(OctopError) as conflict:
        await service.lookup(user, project_id, **domain("k1", domain="other.com"))
    assert conflict.value.code == "SEO_CONFLICT"
    # Within 12 hours the same domain, however written, is reused at no charge.
    reused = await service.lookup(user, project_id, **domain("k2", domain="rival.com"))
    assert reused["cached"] and reused["cost_usd"] == 0 and len(calls) == 2
    assert reused["source_lookup_id"] == found["lookup_id"]
    assert reused["observed_at"] == found["observed_at"] and reused["items"] == found["items"]
    listed = await service.list_lookups(user, project_id, kind="domain")
    assert [row["lookup_id"] for row in listed] == [reused["lookup_id"], found["lookup_id"]]
    assert listed[0]["item_count"] == 1 and "items" not in listed[0]
    assert await service.list_lookups(user, project_id, kind="competitors") == []
    got = await service.get_lookup(user, project_id, found["lookup_id"])
    assert got["items"] == found["items"]


async def test_competitor_lookup_sorts_by_visibility_and_keys_on_the_keyword_set(
    context, monkeypatch
):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content)[0])
        return labs({"items": COMPETITORS})

    project_id = await configured(context, monkeypatch, handler)
    service, _repos, user, _ = context
    found = await service.lookup(
        user, project_id, **competitors("a", [" Short Drama ", "reelshort", "short drama"])
    )
    assert bodies == [
        {
            "keywords": ["reelshort", "short drama"],
            "location_code": 2840,
            "language_code": "en",
            "item_types": ["organic"],
            "limit": 50,
        }
    ]
    assert found["keywords"] == ["reelshort", "short drama"] and found["domain"] is None
    assert [row["domain"] for row in found["items"]] == ["rival.com", "low.com"]
    assert found["items"][0]["keywords_positions"] == {"short drama": [4]}
    assert found["items"][1]["keywords_positions"] == {}
    # The same set in another order and case is the same lookup target.
    reused = await service.lookup(
        user, project_id, **competitors("b", ["REELSHORT", "short drama"])
    )
    assert reused["cached"] and len(bodies) == 1
    await service.lookup(user, project_id, **competitors("c", ["reelshort"]))
    assert len(bodies) == 2


PAGES = [
    {
        "page_address": "https://rival.com/blog/top-dramas",
        "metrics": {
            "organic": {"etv": 812.4, "count": 140, "pos_1": 4, "pos_2_3": 9, "pos_4_10": 30},
            "paid": {"etv": 3},
        },
    },
    {"page_address": "https://rival.com/", "metrics": {}},
    {"metrics": {"organic": {"etv": 5}}},
]


async def test_pages_lookup_buys_top_pages_by_traffic(context, monkeypatch):
    calls = []

    def handler(request):
        calls.append((request.url.path, json.loads(request.content)[0]))
        return labs({"total_count": 420, "items": PAGES}, cost=0.0132)

    project_id = await configured(context, monkeypatch, handler)
    service, _repos, user, _ = context
    found = await service.lookup(user, project_id, **domain("p1", kind="pages"))
    assert calls == [
        (
            "/v3/dataforseo_labs/google/relevant_pages/live",
            {
                "target": "rival.com",
                "location_code": 2840,
                "language_code": "en",
                "limit": 100,
                "order_by": ["metrics.organic.etv,desc"],
            },
        )
    ]
    assert (found["status"], found["kind"], found["domain"], found["overview"]) == (
        "completed",
        "pages",
        "rival.com",
        None,
    )
    assert found["total_count"] == 420 and found["cost_usd"] == pytest.approx(0.0132)
    # A page without an address is dropped; missing metrics stay unknown, not zero.
    assert found["items"] == [
        {
            "url": "https://rival.com/blog/top-dramas",
            "etv": 812.4,
            "keywords": 140,
            "pos_1": 4,
            "pos_2_3": 9,
            "pos_4_10": 30,
        },
        {
            "url": "https://rival.com/",
            "etv": None,
            "keywords": None,
            "pos_1": None,
            "pos_2_3": None,
            "pos_4_10": None,
        },
    ]
    # Pages and the domain overview of the same domain are separate purchases.
    reused = await service.lookup(user, project_id, **domain("p2", kind="pages"))
    assert reused["cached"] and len(calls) == 1
    assert [row["kind"] for row in await service.list_lookups(user, project_id, kind="pages")] == [
        "pages",
        "pages",
    ]
    assert await service.list_lookups(user, project_id, kind="domain") == []


async def test_lookup_rejects_bad_input_before_buying(context, monkeypatch):
    calls = []
    project_id = await configured(
        context, monkeypatch, lambda request: calls.append(1) or labs({"items": []})
    )
    service, _repos, user, other = context
    for bad in (
        domain("a", confirm_paid=False),
        domain("b", domain="not a domain"),
        domain("c", keywords=["x"]),
        competitors("d", []),
        competitors("e", ["  "]),
        competitors("f", ["x"], domain="rival.com"),
        {"kind": "domain", "idempotency_key": "g", "confirm_paid": True},
        {"kind": "pages", "idempotency_key": "i", "confirm_paid": True},
        domain("j", kind="pages", keywords=["x"]),
        competitors("k", ["x"], kind="pages"),
    ):
        with pytest.raises(OctopError):
            await service.lookup(user, project_id, **bad)
    with pytest.raises(OctopError) as forbidden:
        await service.lookup(other, project_id, **domain("h"))
    assert forbidden.value.code == "FORBIDDEN"
    assert calls == []


async def test_lookup_failures_keep_costs_and_empty_results_are_not_reused(context, monkeypatch):
    replies = [
        labs({"items": OVERVIEW}),
        labs(None, task_status=40501, cost=0),
        labs({"items": OVERVIEW}),
        labs({"total_count": 0, "items": []}, cost=0.01),
    ]

    def handler(request):
        if not replies:
            raise httpx.ConnectError("down")
        return replies.pop(0)

    project_id = await configured(context, monkeypatch, handler)
    service, repos, user, _ = context
    # The second call is rejected after the first was charged: both receipts stay.
    failed = await service.lookup(user, project_id, **domain("a"))
    assert (failed["status"], failed["error"]) == ("failed", "provider_rejected")
    assert failed["overview"]["organic_keywords"] == 87 and failed["items"] == []
    assert failed["cost_usd"] == pytest.approx(0.0101) and len(failed["receipts"]) == 2
    empty = await service.lookup(user, project_id, **domain("b"))
    assert (empty["status"], empty["items"], empty["total_count"]) == ("completed", [], 0)
    # Neither a failed nor an empty lookup answers from cache; a lost reply is unknown.
    unknown = await service.lookup(user, project_id, **domain("c"))
    assert (unknown["status"], unknown["cost_usd"], unknown["cost_known_usd"]) == (
        "unknown",
        None,
        0,
    )
    unknown.update(
        status="running", observed_at=(datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    )
    repos.seo_repo.update_lookup(unknown)
    listed = await service.list_lookups(user, project_id)
    assert listed[0]["status"] == "unknown" and listed[0]["error"] == "interrupted"
    with pytest.raises(OctopError) as missing:
        await service.get_lookup(user, project_id, "nope")
    assert missing.value.code == "NOT_FOUND"


BACKLINK_REPLIES = {
    "summary": {
        "target": "rival.com",
        "rank": 41,
        "backlinks": 8123,
        "referring_pages": 6000,
        "referring_domains": 512,
        "broken_backlinks": 7,
        "broken_pages": 2,
        "backlinks_spam_score": 12,
        "info": {"target_spam_score": 3},
        "new_backlinks": 40,
        "lost_backlinks": 11,
        "new_reffering_domains": 5,
        "lost_referring_domains": 2,
    },
    "history": {
        "items": [
            {"date": "2026-09-01 00:00:00 +00:00", "backlinks": 8000, "referring_domains": 500},
            {"date": "2025-11-01 00:00:00 +00:00", "backlinks": 7000, "new_reffering_domains": 9},
            {"backlinks": 1},
        ]
    },
    "backlinks": {
        "total_count": 640,
        "items": [
            {
                "domain_from": "blog.example.org",
                "url_from": "https://blog.example.org/post",
                "url_to": "https://rival.com/",
                "anchor": "Rival",
                "item_type": "anchor",
                "dofollow": True,
                "rank": 30,
                "domain_from_rank": 55,
                "backlink_spam_score": 4,
                "first_seen": "2026-01-02 00:00:00 +00:00",
                "last_seen": "2026-10-20 00:00:00 +00:00",
                "lost_date": None,
            }
        ],
    },
    "referring_domains": {
        "total_count": 512,
        "items": [{"domain": "example.org", "backlinks": 3, "rank": 55, "backlinks_spam_score": 4}],
    },
    "domain_pages_summary": {
        "total_count": 90,
        "items": [{"page": "https://rival.com/", "backlinks": 4000, "referring_domains": 300}],
    },
}


def backlinks_handler(calls, *, fail=None):
    def handler(request):
        section = request.url.path.split("/")[3]
        calls.append((section, json.loads(request.content)[0]))
        if section == fail:
            return labs(None, task_status=40204, cost=0)
        return labs(BACKLINK_REPLIES[section], cost=0.025)

    return handler


async def test_backlinks_lookup_buys_five_sections_at_once(context, monkeypatch):
    calls = []
    project_id = await configured(context, monkeypatch, backlinks_handler(calls))
    service, _repos, user, _ = context
    found = await service.lookup(user, project_id, **domain("b1", kind="backlinks"))
    bodies = dict(calls)
    assert sorted(bodies) == [
        "backlinks",
        "domain_pages_summary",
        "history",
        "referring_domains",
        "summary",
    ]
    common = {
        "target": "rival.com",
        "include_subdomains": True,
        "include_indirect_links": True,
        "exclude_internal_backlinks": True,
        "backlinks_status_type": "live",
        "rank_scale": "one_hundred",
    }
    assert bodies["summary"] == common
    assert bodies["backlinks"] == common | {
        "limit": 100,
        "order_by": ["rank,desc"],
        "mode": "one_per_domain",
        "filters": [["backlink_spam_score", "<", 40], "or", ["backlink_spam_score", "=", None]],
    }
    assert bodies["referring_domains"] == common | {
        "limit": 100,
        "order_by": ["backlinks,desc"],
        "filters": ["backlinks_spam_score", "<=", 40],
    }
    assert bodies["domain_pages_summary"] == common | {"limit": 100, "order_by": ["backlinks,desc"]}
    # A year of history up to yesterday (UTC), on the bare hostname.
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    try:
        year_before = yesterday.replace(year=yesterday.year - 1)
    except ValueError:
        year_before = yesterday.replace(year=yesterday.year - 1, day=28)
    assert bodies["history"] == {
        "target": "rival.com",
        "date_from": year_before.isoformat(),
        "date_to": yesterday.isoformat(),
        "rank_scale": "one_hundred",
    }
    assert (found["status"], found["kind"]) == ("completed", "backlinks")
    assert found["cost_usd"] == pytest.approx(0.125) and len(found["receipts"]) == 5
    # Both spellings of DataForSEO's referring-domain counters are read.
    assert found["overview"] == {
        "rank": 41,
        "backlinks": 8123,
        "referring_pages": 6000,
        "referring_domains": 512,
        "broken_backlinks": 7,
        "broken_pages": 2,
        "backlinks_spam_score": 12,
        "target_spam_score": 3,
        "new_backlinks": 40,
        "lost_backlinks": 11,
        "new_referring_domains": 5,
        "lost_referring_domains": 2,
    }
    assert [row["section"] for row in found["items"]] == [
        "backlink",
        "referring_domain",
        "page",
        "history",
        "history",
    ]
    assert found["items"][0] == {
        "section": "backlink",
        "domain_from": "blog.example.org",
        "url_from": "https://blog.example.org/post",
        "url_to": "https://rival.com/",
        "anchor": "Rival",
        "item_type": "anchor",
        "dofollow": True,
        "rank": 30,
        "domain_from_rank": 55,
        "spam_score": 4,
        "first_seen": "2026-01-02 00:00:00 +00:00",
        "last_seen": "2026-10-20 00:00:00 +00:00",
        "is_lost": False,
        "is_broken": False,
    }
    assert found["items"][1]["spam_score"] == 4 and found["items"][2]["url"] == "https://rival.com/"
    # History is monthly, oldest first; a row without a date is dropped.
    assert [(row["date"], row["new_referring_domains"]) for row in found["items"][3:]] == [
        ("2025-11-01", 9),
        ("2026-09-01", None),
    ]
    reused = await service.lookup(user, project_id, **domain("b2", kind="backlinks"))
    assert reused["cached"] and len(calls) == 5


async def test_backlinks_failure_keeps_the_sections_that_answered(context, monkeypatch):
    calls = []
    project_id = await configured(
        context, monkeypatch, backlinks_handler(calls, fail="referring_domains")
    )
    service, _repos, user, _ = context
    found = await service.lookup(user, project_id, **domain("b1", kind="backlinks"))
    assert (found["status"], found["error"]) == ("failed", "provider_rejected")
    assert found["overview"]["backlinks"] == 8123
    assert "referring_domain" not in {row["section"] for row in found["items"]}
    assert found["cost_usd"] == pytest.approx(0.1) and len(found["receipts"]) == 5
    # A failed lookup is never reused: the next click buys all five again.
    await service.lookup(user, project_id, **domain("b2", kind="backlinks"))
    assert len(calls) == 10
