"""Prompt lookups: model choice, per-model isolation, unbillable refusals and 12-hour reuse of
fully answered lookups."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from octop.infra.errors import OctopError
from octop.infra.seo.provider import pick_llm_model_name, supports_search_country
from tests.unit.seo.test_lookups import labs
from tests.unit.seo.test_service import configured

CATALOGS = {
    "chat_gpt": ["gpt-5.5", "gpt-5.6-luna", "gpt-5.6-terra"],
    "claude": ["claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6"],
    "gemini": ["gemini-2.5-pro", "gemini-3-pro", "gemini-3-flash"],
    "perplexity": ["sonar", "sonar-reasoning-pro"],
}
ANSWER = {
    "model_name": None,
    "items": [
        {"type": "reasoning", "sections": [{"text": "hidden"}]},
        {
            "type": "message",
            "sections": [
                {
                    "text": "ReelShort leads.",
                    "annotations": [
                        {"title": "Apps", "url": "https://apps.test/top"},
                        {"title": "Dup", "url": "https://apps.test/top"},
                        {"title": "Bad", "url": "javascript:alert(1)"},
                    ],
                },
                {"text": "ShortsBinge is free.", "annotations": None},
            ],
        },
    ],
    "fan_out_queries": ["best short drama apps", 7],
    "output_tokens": 812.6,
    "web_search": True,
}


def catalog(names):
    task = {"status_code": 20000, "result": [{"model_name": name} for name in names]}
    return httpx.Response(200, json={"status_code": 20000, "tasks": [task]})


def llm_handler(calls, *, fail=(), catalogs=CATALOGS):
    def handler(request):
        model = request.url.path.split("/")[3]
        if request.method == "GET":
            calls.append(("models", model, None))
            if model not in catalogs:
                return httpx.Response(500)
            return catalog(catalogs[model])
        body = json.loads(request.content)[0]
        calls.append(("live", model, body))
        if model in fail:
            return labs(None, task_status=40501, cost=0.004)
        return labs(ANSWER | {"model_name": body["model_name"] + "-2026"}, cost=0.03)

    return handler


def prompt(key, **extra):
    return {
        "prompt": "  Best   short drama apps\n",
        "models": ["claude", "chat_gpt"],
        "country": "US",
        "idempotency_key": key,
        "confirm_paid": True,
    } | extra


def test_model_choice_follows_the_catalog_and_country_support_is_per_model():
    # A pin wins while listed; otherwise the highest plain alias, never a tier or dated name.
    assert pick_llm_model_name("chat_gpt", CATALOGS["chat_gpt"]) == "gpt-5.6-luna"
    assert pick_llm_model_name("chat_gpt", ["gpt-5.5", "gpt-5.10", "gpt-5.11-mini"]) == "gpt-5.10"
    assert pick_llm_model_name("claude", ["claude-sonnet-4-6", "claude-sonnet-5-5"]) == (
        "claude-sonnet-5-5"
    )
    assert pick_llm_model_name("gemini", CATALOGS["gemini"]) == "gemini-3-pro"
    # No flagship listed: a listed fallback, else the first fallback (then refused unsent).
    assert pick_llm_model_name("perplexity", ["sonar-pro", "sonar"]) == "sonar-pro"
    assert pick_llm_model_name("chat_gpt", ["gpt-4o"]) == "gpt-5.6-luna"
    assert not supports_search_country("gemini", "US")
    assert supports_search_country("claude", "GB") and not supports_search_country("claude", "BG")
    assert supports_search_country("chat_gpt", "BG") and supports_search_country("perplexity", "BG")


async def test_prompt_lookup_asks_each_model_once_and_reuses_for_12_hours(context, monkeypatch):
    calls = []
    project_id = await configured(context, monkeypatch, llm_handler(calls))
    service, _repos, user, _ = context
    found = await service.prompt_lookup(user, project_id, **prompt("p1"))
    assert found["request"] == {
        "prompt": "Best   short drama apps",
        "models": ["chat_gpt", "claude"],
        "web_search": True,
        "country": "US",
    }
    bodies = {model: body for kind, model, body in calls if kind == "live"}
    assert bodies["chat_gpt"] == {
        "user_prompt": "Best   short drama apps",
        "model_name": "gpt-5.6-luna",
        "web_search": True,
        "max_output_tokens": 4096,
        "web_search_country_iso_code": "US",
    }
    # Only Claude can be made to search; its pin wins over the newer name it lists.
    assert bodies["claude"] == bodies["chat_gpt"] | {
        "model_name": "claude-sonnet-5",
        "force_web_search": True,
    }
    assert found["status"] == "completed" and found["error"] is None
    assert [row["model"] for row in found["items"]] == ["chat_gpt", "claude"]
    assert found["items"][0] == {
        "model": "chat_gpt",
        "model_name": "gpt-5.6-luna-2026",
        "status": "completed",
        "error": None,
        "text": "ReelShort leads.\n\nShortsBinge is free.",
        "citations": [{"url": "https://apps.test/top", "domain": "apps.test", "title": "Apps"}],
        "fan_out_queries": ["best short drama apps"],
        "output_tokens": 813,
        "web_search": True,
        "cost_usd": 0.03,
    }
    assert found["cost_usd"] == pytest.approx(0.06) and len(found["receipts"]) == 2
    assert len(calls) == 4
    again = await service.prompt_lookup(user, project_id, **prompt("p1"))
    assert again["lookup_id"] == found["lookup_id"] and len(calls) == 4
    with pytest.raises(OctopError) as conflict:
        await service.prompt_lookup(user, project_id, **prompt("p1", prompt="Other"))
    assert conflict.value.code == "SEO_CONFLICT"
    # Whitespace and model order do not make a new question; it is reused at no charge.
    reused = await service.prompt_lookup(
        user,
        project_id,
        **prompt("p2", prompt="Best short drama apps", models=["chat_gpt", "claude"]),
    )
    assert reused["cached"] and reused["cost_usd"] == 0 and len(calls) == 4
    assert reused["items"] == found["items"]
    listed = await service.list_ai_lookups(user, project_id)
    assert [row["lookup_id"] for row in listed] == [reused["lookup_id"], found["lookup_id"]]
    assert listed[0]["item_count"] == 2 and "items" not in listed[0]
    # Competitor research never lists or opens a prompt lookup.
    assert await service.list_lookups(user, project_id) == []
    with pytest.raises(OctopError) as missing:
        await service.get_lookup(user, project_id, found["lookup_id"])
    assert missing.value.code == "NOT_FOUND"
    with pytest.raises(OctopError):
        await service.list_lookups(user, project_id, kind="prompt")
    got = await service.get_ai_lookup(user, project_id, found["lookup_id"])
    assert got["items"] == found["items"]


async def test_prompt_lookup_keeps_answers_when_a_model_fails(context, monkeypatch):
    calls = []
    project_id = await configured(context, monkeypatch, llm_handler(calls, fail=("claude",)))
    service, _repos, user, _ = context
    found = await service.prompt_lookup(
        user, project_id, **prompt("p1", models=["chat_gpt", "claude", "gemini"])
    )
    # Gemini takes no country: refused before its catalog or answer is requested.
    assert [(kind, model) for kind, model, _ in calls if model == "gemini"] == []
    chat, claude, gemini = found["items"]
    assert (chat["status"], chat["text"][:9]) == ("completed", "ReelShort")
    assert (claude["status"], claude["error"], claude["cost_usd"]) == (
        "failed",
        "provider_rejected",
        0.004,
    )
    assert (gemini["status"], gemini["error"], gemini["cost_usd"]) == (
        "failed",
        "unsupported_country",
        0.0,
    )
    # A failed request fails the lookup, which keeps the answer but is not reused.
    assert (found["status"], found["error"]) == ("failed", "provider_rejected")
    assert found["cost_usd"] == pytest.approx(0.034) and len(found["receipts"]) == 2
    again = await service.prompt_lookup(
        user, project_id, **prompt("p2", models=["gemini", "claude", "chat_gpt"])
    )
    assert not again["cached"] and len(calls) == 8
    # A free refusal would be refused again, so it fails nothing and the lookup is reused.
    refused = await service.prompt_lookup(
        user, project_id, **prompt("p3", models=["chat_gpt", "gemini"])
    )
    assert (refused["status"], refused["error"]) == ("completed", None)
    reused = await service.prompt_lookup(
        user, project_id, **prompt("p4", models=["gemini", "chat_gpt"])
    )
    assert reused["cached"] and len(calls) == 10


async def test_prompt_lookup_never_sends_a_model_the_catalog_does_not_list(context, monkeypatch):
    calls = []
    catalogs = {"chat_gpt": ["gpt-4o"], "claude": CATALOGS["claude"]}
    project_id = await configured(
        context, monkeypatch, llm_handler(calls, fail=("claude",), catalogs=catalogs)
    )
    service, _repos, user, _ = context
    # The catalog lists no known ChatGPT name: refused unsent. Perplexity's catalog is down:
    # its snapshot names are used. Without web search no country or forced search is sent.
    found = await service.prompt_lookup(
        user,
        project_id,
        **prompt("p1", models=["chat_gpt", "perplexity", "claude"], web_search=False),
    )
    assert found["request"]["country"] is None
    sent = {model: body for kind, model, body in calls if kind == "live"}
    assert set(sent) == {"claude", "perplexity"}
    assert sent["perplexity"]["model_name"] == "sonar-reasoning-pro"
    assert sent["claude"] == {
        "user_prompt": "Best   short drama apps",
        "model_name": "claude-sonnet-5",
        "web_search": False,
        "max_output_tokens": 4096,
    }
    assert [row["error"] for row in found["items"]] == ["unknown_model", "provider_rejected", None]
    # The lookup reports the request that failed, not the free refusal before it.
    assert (found["status"], found["error"]) == ("failed", "provider_rejected")
    # Failed lookups are never reused.
    failed = await service.prompt_lookup(
        user, project_id, **prompt("p2", models=["chat_gpt", "claude"], web_search=False)
    )
    assert (failed["status"], failed["error"]) == ("failed", "provider_rejected")
    again = await service.prompt_lookup(
        user, project_id, **prompt("p3", models=["chat_gpt", "claude"], web_search=False)
    )
    assert not again["cached"] and again["lookup_id"] != failed["lookup_id"]


async def test_prompt_lookup_rejects_bad_input_and_settles_after_six_minutes(context, monkeypatch):
    calls = []
    project_id = await configured(context, monkeypatch, llm_handler(calls))
    service, repos, user, _ = context
    for bad in (
        prompt("a", confirm_paid=False),
        prompt("b", prompt="   "),
        prompt("c", prompt="x" * 501),
        prompt("d", models=[]),
        prompt("e", models=["chat_gpt", "claude", "gemini", "perplexity", "chat_gpt"]),
        prompt("f", models=["llama"]),
        prompt("g", country="us"),
        prompt("h", domain="rival.com"),
    ):
        with pytest.raises(OctopError):
            await service.prompt_lookup(user, project_id, **bad)
    assert calls == []
    found = await service.prompt_lookup(user, project_id, **prompt("p1"))
    found.update(
        status="running", observed_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    )
    repos.seo_repo.update_lookup(found)
    assert (await service.list_ai_lookups(user, project_id))[0]["status"] == "running"
    found["observed_at"] = (datetime.now(UTC) - timedelta(minutes=7)).isoformat()
    repos.seo_repo.update_lookup(found)
    settled = await service.get_ai_lookup(user, project_id, found["lookup_id"])
    assert (settled["status"], settled["error"]) == ("unknown", "interrupted")
