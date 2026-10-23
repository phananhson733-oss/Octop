"""Brand lookups: LLM Mentions requests, shaping, partial failures and 12-hour reuse."""

import json

import pytest

from octop.infra.errors import OctopError
from tests.unit.seo.test_lookups import labs
from tests.unit.seo.test_service import configured

SAMPLE = [
    {
        "question": "best short drama apps",
        "ai_search_volume": 900,
        "sources": [
            {"url": "https://example.com/a", "title": "Example A"},
            {"url": "javascript:alert(1)", "title": "Bad"},
        ],
        "monthly_searches": [
            {"year": 2026, "month": 9, "search_volume": 100},
            {"year": 2026, "month": 8, "search_volume": 50},
            {"year": 2026, "month": 7, "search_volume": None},
        ],
        "brand_entities": [{"title": "Example"}, {"title": None}],
        "first_response_at": "2026-05-01 00:00:00 +00:00",
        "last_response_at": "2026-10-01 00:00:00 +00:00",
    },
    {"question": None, "ai_search_volume": 5},
]


def mentions_handler(calls, *, fail=None):
    def handler(request):
        endpoint = request.url.path.split("/")[4]
        body = json.loads(request.content)[0]
        platform = body["platform"]
        calls.append((endpoint, body))
        if (platform, endpoint) == fail:
            return labs(None, task_status=40501, cost=0)
        group = {"key": platform, "mentions": 10.4 if platform == "chat_gpt" else 5}
        if endpoint == "aggregated_metrics":
            volume = 1000 if platform == "chat_gpt" else 500
            result = {"total": {"platform": [group | {"ai_search_volume": volume}]}}
        elif endpoint == "top_pages":
            pages = [
                {"key": "https://example.com/a", "platform": [group | {"ai_search_volume": 300}]},
                {"key": "javascript:alert(1)", "platform": [group]},
            ]
            result = {"items": pages}
        elif endpoint == "search":
            result = {"items": SAMPLE}
        else:
            result = {
                "items": [
                    {"key": "example.com", "platform": [{"key": platform, "mentions": 6}]},
                    {"key": "RIVAL", "platform": [{"key": platform, "mentions": 2}]},
                    {"key": "unrequested", "platform": [{"key": platform, "mentions": 90}]},
                ]
            }
        return labs(result, cost=0.101)

    return handler


def brand(key, **extra):
    return {
        "target": "https://www.Example.com/",
        "competitors": ["Rival", "rival", "example.com", "Other Brand"],
        "idempotency_key": key,
        "confirm_paid": True,
    } | extra


async def test_brand_lookup_buys_both_platforms_and_compares_competitors(context, monkeypatch):
    calls = []
    project_id = await configured(context, monkeypatch, mentions_handler(calls))
    service, _repos, user, _ = context
    found = await service.brand_lookup(user, project_id, **brand("b1"))
    # A domain is detected and normalized; competitors are deduped and never the target.
    assert found["request"] == {
        "target": "example.com",
        "target_type": "domain",
        "competitors": ["Rival", "Other Brand"],
    }
    assert len(calls) == 8 and found["status"] == "completed"
    domain = {
        "domain": "example.com",
        "include_subdomains": True,
        "search_filter": "include",
        "search_scope": ["any"],
    }
    market = {"location_code": 2840, "language_code": "en"}
    bodies = {(body["platform"], endpoint): body for endpoint, body in calls}
    assert bodies[("google", "aggregated_metrics")] == market | {
        "target": [domain],
        "platform": "google",
        "internal_list_limit": 20,
    }
    assert bodies[("google", "top_pages")] == market | {
        "target": [domain],
        "platform": "google",
        "links_scope": "sources",
        "items_list_limit": 10,
        "internal_list_limit": 5,
    }
    assert bodies[("chat_gpt", "search")]["limit"] == 100
    assert bodies[("chat_gpt", "cross_aggregated_metrics")]["targets"] == [
        {"aggregation_key": "example.com", "target": [domain]},
        {
            "aggregation_key": "Rival",
            "target": [
                {
                    "keyword": "Rival",
                    "search_filter": "include",
                    "search_scope": ["any", "brand_entities"],
                    "match_type": "word_match",
                }
            ],
        },
        {
            "aggregation_key": "Other Brand",
            "target": [
                {
                    "keyword": "Other Brand",
                    "search_filter": "include",
                    "search_scope": ["any", "brand_entities"],
                    "match_type": "word_match",
                }
            ],
        },
    ]
    overview = found["overview"]
    # A US English project counts ChatGPT, whose data is US English only.
    assert overview["chatgpt_counted"] is True
    assert (overview["total_mentions"], overview["total_ai_search_volume"]) == (15, 1500)
    assert overview["platforms"] == [
        {"platform": "chat_gpt", "status": "completed", "mentions": 10, "ai_search_volume": 1000},
        {"platform": "google", "status": "completed", "mentions": 5, "ai_search_volume": 500},
    ]
    assert overview["monthly_volume"] == [
        {"year": 2026, "month": 8, "volume": 100},
        {"year": 2026, "month": 9, "volume": 200},
    ]
    # Unrequested keys never change the share; a brand without data stays unknown.
    assert overview["share_of_voice"] == {
        "platforms": ["chat_gpt", "google"],
        "entries": [
            {"label": "example.com", "is_target": True, "mentions": 12, "share_pct": 75.0},
            {"label": "Rival", "is_target": False, "mentions": 4, "share_pct": 25.0},
            {"label": "Other Brand", "is_target": False, "mentions": None, "share_pct": None},
        ],
    }
    sections = [(row["section"], row["platform"]) for row in found["items"]]
    assert sections == [
        ("page", "chat_gpt"),
        ("page", "google"),
        ("query", "chat_gpt"),
        ("query", "google"),
    ]
    assert found["items"][0] == {
        "section": "page",
        "platform": "chat_gpt",
        "url": "https://example.com/a",
        "domain": "example.com",
        "mentions": 10,
        "captured_volume": 300,
        "keywords": [{"question": "best short drama apps", "ai_search_volume": 900}],
    }
    assert found["items"][2] == {
        "section": "query",
        "platform": "chat_gpt",
        "question": "best short drama apps",
        "ai_search_volume": 900,
        "first_seen_at": "2026-05-01 00:00:00 +00:00",
        "last_seen_at": "2026-10-01 00:00:00 +00:00",
        "cited_sources": [
            {"url": "https://example.com/a", "domain": "example.com", "title": "Example A"}
        ],
        "brands_mentioned": ["Example"],
    }
    assert found["cost_usd"] == pytest.approx(0.808) and len(found["receipts"]) == 8
    # The same target and competitors, however written, are reused at no charge.
    reused = await service.brand_lookup(
        user,
        project_id,
        **brand("b2", target="example.com", competitors=["other brand", "RIVAL"]),
    )
    assert reused["cached"] and reused["cost_usd"] == 0 and len(calls) == 8
    assert [row["lookup_id"] for row in await service.list_ai_lookups(user, project_id)] == [
        reused["lookup_id"],
        found["lookup_id"],
    ]
    assert await service.list_ai_lookups(user, project_id, kind="prompt") == []
    with pytest.raises(OctopError) as conflict:
        await service.brand_lookup(user, project_id, **brand("b1", target="Example"))
    assert conflict.value.code == "SEO_CONFLICT"


async def test_brand_lookup_outside_the_us_keeps_chatgpt_apart_and_partial_answers(
    context, monkeypatch
):
    calls = []
    await configured(context, monkeypatch, mentions_handler(calls, fail=("google", "search")))
    service, repos, user, _ = context
    repos.agent_repo.create(agent_id="seo-gb", user_id=user, name="SEO GB")
    project = await service.create_project(
        user, name="UK", domain="example.co.uk", agent_id="seo-gb", location_code=2826
    )
    found = await service.brand_lookup(
        user, project["project_id"], **brand("b1", target="Example Brand", competitors=[])
    )
    assert found["request"] == {
        "target": "Example Brand",
        "target_type": "keyword",
        "competitors": [],
    }
    # No competitors: no comparison is bought.
    assert len(calls) == 6 and "cross_aggregated_metrics" not in {e for e, _ in calls}
    markets = {(body["platform"], body["location_code"]) for _, body in calls}
    assert markets == {("chat_gpt", 2840), ("google", 2826)}
    overview = found["overview"]
    assert overview["chatgpt_counted"] is False
    assert (overview["total_mentions"], overview["total_ai_search_volume"]) == (5, 500)
    # ChatGPT's sample is shown but kept out of the trend; Google's sample failed.
    assert overview["monthly_volume"] == []
    assert overview["share_of_voice"] is None
    assert [(row["section"], row["platform"]) for row in found["items"]] == [
        ("page", "chat_gpt"),
        ("page", "google"),
        ("query", "chat_gpt"),
    ]
    assert found["items"][1]["keywords"] == []
    assert (found["status"], found["error"]) == ("failed", "provider_rejected")
    assert found["cost_usd"] == pytest.approx(0.505) and len(found["receipts"]) == 6
    # A failed lookup is never reused.
    again = await service.brand_lookup(
        user, project["project_id"], **brand("b2", target="Example Brand", competitors=[])
    )
    assert not again["cached"] and len(calls) == 12
    # Share of voice leaves ChatGPT out here, so its comparison is not bought.
    compared = await service.brand_lookup(
        user, project["project_id"], **brand("b3", target="Example Brand", competitors=["Rival"])
    )
    bought = [(body["platform"], endpoint) for endpoint, body in calls[12:]]
    assert len(bought) == 7 and ("chat_gpt", "cross_aggregated_metrics") not in bought
    assert compared["overview"]["share_of_voice"]["platforms"] == ["google"]


async def test_brand_lookup_rejects_bad_input_before_buying(context, monkeypatch):
    calls = []
    project_id = await configured(context, monkeypatch, mentions_handler(calls))
    service, _repos, user, _ = context
    for bad in (
        brand("a", confirm_paid=False),
        brand("b", target="   "),
        brand("c", target="x" * 251),
        brand("d", competitors=["a", "b", "c", "d", "e", "f"]),
        brand("e", competitors=[" "]),
        brand("f", domain="example.com"),
    ):
        with pytest.raises(OctopError):
            await service.brand_lookup(user, project_id, **bad)
    assert calls == []
