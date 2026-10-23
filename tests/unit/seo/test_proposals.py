"""The chat's confirm card buys only a signed proposal, and a check only the list it showed."""

import json

import pytest

from octop.infra.errors import OctopError
from octop.infra.seo import proposals
from octop.infra.seo.ai_visibility import SeoAiService
from octop.infra.seo.rank import SeoRankService
from tests.unit.seo.test_rank import drained, organic, serp
from tests.unit.seo.test_service import configured, response


async def test_the_card_buys_only_the_signed_proposal(context, monkeypatch):
    calls = []

    def supplier(request):
        calls.append(json.loads(request.content))
        return response([{"keyword": "crm software", "keyword_info": {"search_volume": 900}}])

    project_id = await configured(context, monkeypatch, supplier)
    service, _repos, user, _other = context
    purchase = {
        "kind": "research",
        "project_id": project_id,
        "idempotency_key": "agent-1",
        "input": {"keywords": ["crm"], "mode": "ideas", "result_limit": 150},
    }
    signature = await proposals.sign(service, purchase)
    bought = await proposals.confirm(service, user, project_id, purchase, signature)
    assert bought["kind"] == "research"
    assert bought["status"] in ("running", "completed")
    # A second click reads the first attempt.
    again = await proposals.confirm(service, user, project_id, purchase, signature)
    assert again["record_id"] == bought["record_id"]
    assert len(calls) == 1
    for changed, path_project in (
        (purchase | {"input": purchase["input"] | {"result_limit": 500}}, project_id),
        (purchase | {"idempotency_key": "agent-2"}, project_id),
        (purchase, "another-project"),
    ):
        with pytest.raises(OctopError) as refused:
            await proposals.confirm(service, user, path_project, changed, signature)
        assert refused.value.code == "FORBIDDEN"
    with pytest.raises(OctopError) as unsigned:
        await proposals.confirm(service, user, project_id, purchase, "0" * 64)
    assert unsigned.value.code == "FORBIDDEN"
    assert len(calls) == 1


async def test_a_whole_dollar_estimate_survives_the_card(context):
    service, _repos, _user, _other = context
    # 100 keywords on both devices at depth 30 cost at most $1.0, which the browser sends as 1.
    purchase = {"kind": "rank_check", "input": {"max_cost_usd": 1.0}}
    signature = await proposals.sign(service, purchase)
    assert await proposals.sign(service, json.loads(json.dumps(purchase).replace("1.0", "1"))) == (
        signature
    )
    assert await proposals.sign(service, purchase | {"input": {"max_cost_usd": 1.5}}) != signature


async def test_a_check_buys_only_the_list_the_card_showed(context, monkeypatch):
    project_id = await configured(
        context, monkeypatch, lambda request: serp([organic(1, "example.com")])
    )
    service, _repos, user, _other = context
    ranks = SeoRankService(service)
    tracker_id = (await ranks.create_tracker(user, project_id, depth=20, keywords=["crm", "erp"]))[
        "tracker"
    ]["tracker_id"]

    async def proposal(key):
        found = await ranks.results(user, project_id, tracker_id)
        purchase = {
            "kind": "rank_check",
            "project_id": project_id,
            "idempotency_key": key,
            "input": {"max_cost_usd": found["tracker"]["estimate_usd"]},
            "tracker_id": tracker_id,
            "content_hash": proposals.rank_content(found),
        }
        return purchase, await proposals.sign(service, purchase)

    swapped, swapped_signature = await proposal("agent-swapped")
    # Same count and price, different keyword: what the card showed is no longer bought.
    erp = next(
        row["keyword_id"]
        for row in (await ranks.results(user, project_id, tracker_id))["rows"]
        if row["keyword"] == "erp"
    )
    await ranks.remove_keyword(user, project_id, tracker_id, erp)
    await ranks.add_keywords(user, project_id, tracker_id, keywords=["hr software"])
    with pytest.raises(OctopError) as changed:
        await proposals.confirm(service, user, project_id, swapped, swapped_signature)
    assert changed.value.code == "SEO_CONFLICT"
    current, signature = await proposal("agent-current")
    bought = await proposals.confirm(service, user, project_id, current, signature)
    assert bought["kind"] == "rank_check"
    await drained()

    ai = SeoAiService(service)
    await ai.add_prompts(user, project_id, prompts=["best crm?", "cheap crm?"])
    found = await ai.results(user, project_id)
    purchase = {
        "kind": "ai_check",
        "project_id": project_id,
        "idempotency_key": "agent-ai",
        "input": {"max_cost_usd": found["settings"]["estimate_usd"]},
        "content_hash": proposals.ai_content(found),
    }
    signature = await proposals.sign(service, purchase)
    cheap = next(p["prompt_id"] for p in found["prompts"] if p["prompt"] == "cheap crm?")
    await ai.remove_prompt(user, project_id, cheap)
    await ai.add_prompts(user, project_id, prompts=["crm with our customer list?"])
    with pytest.raises(OctopError) as replaced:
        await proposals.confirm(service, user, project_id, purchase, signature)
    assert replaced.value.code == "SEO_CONFLICT"
