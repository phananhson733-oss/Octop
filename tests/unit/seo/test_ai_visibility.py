"""AI visibility: answer parsing, brand matching, prompts and paid checks in the background."""

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

import octop.infra.seo.ai_visibility as ai
from octop.infra.errors import OctopError
from octop.infra.seo.ai_matching import (
    brand_names,
    match_brand,
    mention_spans,
    owned_domain,
    prompt_is_branded,
)
from octop.infra.seo.ai_visibility import SeoAiService, estimate_usd
from octop.infra.seo.provider import (
    DataForSeoClient,
    ProviderFailure,
    map_ai_answer,
    markdown_to_text,
)
from tests.unit.seo.test_service import configured

CHATGPT = "/v3/ai_optimization/chat_gpt/llm_scraper/live/advanced"
GEMINI = "/v3/ai_optimization/gemini/llm_scraper/live/advanced"
SERP = "/v3/serp/google/organic/live/advanced"


def reply(result, *, cost=0.004, task_status=20000, message="Ok."):
    task = {"id": "t", "status_code": task_status, "status_message": message, "cost": cost}
    task["result"] = [result] if task_status == 20000 and result is not None else None
    return httpx.Response(200, json={"status_code": 20000, "tasks": [task]})


async def drained():
    await asyncio.gather(*list(ai._CHECKS))


def check(key, **extra):
    return {"idempotency_key": key, "confirm_paid": True, "max_cost_usd": 1} | extra


async def setup(context, monkeypatch, handler):
    project_id = await configured(context, monkeypatch, handler)
    service = context[0]
    await service.update_context(
        context[2],
        project_id,
        [{"add_competitors": [{"domain": "rival.com", "name": "Rival"}, {"domain": "beta.io"}]}],
        author="user",
    )
    return SeoAiService(service), project_id


def test_answers_keep_prose_and_safe_citations():
    text = markdown_to_text(
        "## Best apps\n- **Example** [1](https://example.com/a) and [Rival](https://rival.com)"
        " [example.com](https://www.example.com/) ![logo](https://cdn.example.com/x.png)"
    )
    # Citation pills and images are sources, not prose naming a brand.
    assert text == "Best apps\nExample  and Rival  "
    found = map_ai_answer(
        {
            "markdown": "Try **Example**",
            "sources": [
                {"url": "https://Example.com/a?utm_source=x&q=1#top", "title": None},
                {"url": "https://example.com/a?q=1", "title": "A"},
                {"url": "javascript:alert(1)", "title": "x"},
                {"url": "https://user:pw@evil.com/", "title": "x"},
                {"url": "https://rival.com", "title": 5},
                "not a source",
            ],
        },
        "chatgpt",
    )
    assert found["answer_markdown"] == "Try **Example**" and found["answer_text"] == "Try Example"
    assert found["citations"] == [
        {"url": "https://example.com/a?q=1", "domain": "example.com", "title": "A", "position": 1},
        {"url": "https://rival.com/", "domain": "rival.com", "title": None, "position": 2},
    ]
    overview = map_ai_answer(
        {
            "items": [
                {"type": "organic", "domain": "rival.com"},
                {
                    "type": "ai_overview",
                    "markdown": "Example is popular",
                    "references": [{"url": "https://a.com/1"}],
                    "items": [{"references": [{"url": "https://b.com/2"}]}, {}],
                },
            ]
        },
        "google_ai_overview",
    )
    assert overview["answer_text"] == "Example is popular"
    assert [c["domain"] for c in overview["citations"]] == ["a.com", "b.com"]
    # No AI answer shown: an empty or markup-only answer, or a SERP without an overview.
    for result, engine in (
        ({}, "gemini"),
        ({"markdown": "![x](https://x.com/i.png)"}, "chatgpt"),
        ({"items": [{"type": "organic"}]}, "google_ai_overview"),
    ):
        assert map_ai_answer(result, engine) == {
            "answer_markdown": None,
            "answer_text": "",
            "citations": [],
        }


async def test_a_malformed_answer_is_a_failed_purchase():
    for result in ({"markdown": 5}, {"markdown": "x", "sources": "nope"}):
        client = DataForSeoClient(
            "l", "p", transport=httpx.MockTransport(lambda request, r=result: reply(r))
        )
        with pytest.raises(ProviderFailure) as failed:
            await client.ai_answer(
                engine="chatgpt", prompt="x", location_code=2840, language_code="en"
            )
        assert str(failed.value) == "malformed_result" and failed.value.cost == 0.004


def test_brands_are_matched_as_whole_words():
    assert mention_spans(
        "Example, ExampleCorp, example.com or https://example.com/x",
        [
            "Example",
            "example.com",
        ],
    ) == [(0, 7), (22, 33)]
    # Canonically equivalent Unicode matches; whitespace inside a name may vary.
    assert mention_spans("Café Ole and Café   Ole", ["Café Ole"]) == [(0, 9), (14, 24)]
    assert mention_spans("me at x@example.com", ["example.com"]) == []
    assert brand_names({"name": "rival.co.uk", "domain": "rival.co.uk"})[-1] == "rival"
    assert brand_names({"name": "Rival", "domain": "rival.com"}) == ["Rival", "rival.com"]
    assert len(brand_names({"name": "blog.rival.com", "domain": "blog.rival.com"})) == 2
    assert owned_domain("WWW.Blog.Example.com", "example.com")
    assert not owned_domain("notexample.com", "example.com")
    matched = match_brand(
        "Some say rival is fine",
        [{"domain": "www.rival.com"}],
        {
            "name": "rival.com",
            "domain": "rival.com",
            "own": False,
        },
    )
    assert matched == {
        "name": "rival.com",
        "domain": "rival.com",
        "own": False,
        "mentioned": True,
        "cited": True,
        "first_mention": 9,
    }
    own = {"name": "Example", "domain": "example.com"}
    assert prompt_is_branded("is EXAMPLE.com legit?", own)
    assert prompt_is_branded("Example vs Rival", own)
    assert not prompt_is_branded(
        "example.com.evil.net review", {"name": "Zed", "domain": own["domain"]}
    )
    assert not prompt_is_branded("best short drama apps", own)


async def test_prompts_and_engines(context, monkeypatch):
    service, project_id = await setup(context, monkeypatch, lambda request: reply({}))
    _, _repos, user, other = context
    empty = await service.results(user, project_id)
    assert empty["settings"]["engines"] == ["chatgpt", "gemini"]
    assert (empty["settings"]["prompt_count"], empty["settings"]["estimate_usd"]) == (0, 0)
    assert empty["run"] is None and empty["rows"] == [] and empty["runs"] == []
    assert [(b["name"], b["domain"], b["own"]) for b in empty["brands"]] == [
        ("Example", "example.com", True),
        ("beta.io", "beta.io", False),
        ("Rival", "rival.com", False),
    ]
    added = await service.add_prompts(
        user,
        project_id,
        topic=" Apps ",
        prompts=[" best short drama apps ", "best short drama apps", "is example.com legit?"],
    )
    assert added == {"added": 2}
    assert await service.add_prompts(
        user, project_id, prompts=["best short drama apps", "free drama apps"]
    ) == {"added": 1}
    found = await service.set_engines(
        user, project_id, engines=["google_ai_overview", "chatgpt", "chatgpt"]
    )
    assert found["settings"]["engines"] == ["chatgpt", "google_ai_overview"]
    assert found["settings"]["estimate_usd"] == pytest.approx(estimate_usd(3, ["a", "b"]))
    assert estimate_usd(3, ["a", "b"]) == pytest.approx(0.024)
    assert [(p["topic"], p["prompt"], p["branded"]) for p in found["prompts"]] == [
        ("Apps", "best short drama apps", False),
        ("Apps", "is example.com legit?", True),
        ("General", "free drama apps", False),
    ]
    for bad in ({"engines": []}, {"engines": ["claude"]}):
        with pytest.raises(OctopError) as invalid:
            await service.set_engines(user, project_id, **bad)
        assert invalid.value.code == "SEO_INVALID_INPUT"
    with pytest.raises(OctopError):
        await service.add_prompts(user, project_id, prompts=["  "])
    monkeypatch.setattr(ai, "MAX_PROMPTS", 3)
    with pytest.raises(OctopError) as full:
        await service.add_prompts(user, project_id, prompts=["one more"])
    assert full.value.code == "SEO_INVALID_INPUT"
    prompt_id = found["prompts"][2]["prompt_id"]
    await service.remove_prompt(user, project_id, prompt_id)
    with pytest.raises(OctopError) as gone:
        await service.remove_prompt(user, project_id, prompt_id)
    assert gone.value.code == "NOT_FOUND"
    with pytest.raises(OctopError) as forbidden:
        await service.results(other, project_id)
    assert forbidden.value.code == "FORBIDDEN"
    with pytest.raises(OctopError):
        await service.results(user, project_id, branded="maybe")


async def test_a_long_prompt_in_any_script_is_saved(context, monkeypatch):
    service, project_id = await setup(context, monkeypatch, lambda request: reply({}))
    user = context[2]
    # Not repetitive, so PostgreSQL cannot compress it below its btree row limit.
    prompt = "".join(chr(0x4E00 + (i * 7919) % 20000) for i in range(2000))
    assert await service.add_prompts(user, project_id, prompts=[prompt]) == {"added": 1}
    assert await service.add_prompts(user, project_id, prompts=[prompt]) == {"added": 0}


async def test_a_check_buys_one_answer_per_prompt_and_engine(context, monkeypatch):
    bodies = []

    def handler(request):
        body = json.loads(request.content)[0]
        bodies.append((request.url.path, body))
        if request.url.path == CHATGPT and body["keyword"].startswith("best"):
            return reply(
                {
                    "markdown": "Rival leads, then **Example** [1](https://example.com/top).",
                    "sources": [{"url": "https://example.com/top"}, {"url": "https://news.com/a"}],
                }
            )
        if request.url.path == CHATGPT:
            return reply(
                {"markdown": "Rival and beta.io", "sources": [{"url": "https://rival.com/"}]}
            )
        if request.url.path == GEMINI:
            return reply({"markdown": None, "sources": None})
        return reply(
            {
                "items": [
                    {
                        "type": "ai_overview",
                        "markdown": "Example is a short drama app",
                        "references": [{"url": "https://www.example.com/"}],
                    }
                ]
            }
        )

    service, project_id = await setup(context, monkeypatch, handler)
    user = context[2]
    await service.add_prompts(
        user, project_id, prompts=["best 100% drama + apps", "example.com reviews", "free apps"]
    )
    settings = (
        await service.set_engines(
            user, project_id, engines=["chatgpt", "gemini", "google_ai_overview"]
        )
    )["settings"]
    assert settings["estimate_usd"] == pytest.approx(9 * 0.004)
    started = await service.start_check(
        user, project_id, **check("k1", max_cost_usd=settings["estimate_usd"])
    )
    assert (started["status"], started["answers_total"], started["answers_done"]) == (
        "running",
        9,
        0,
    )
    await drained()
    assert len(bodies) == 9
    sent = {(path, body["keyword"]): body for path, body in bodies}
    # DataForSEO decodes %-escapes, so a literal % and + are escaped.
    assert sent[(CHATGPT, "best 100%25 drama %2B apps")] == {
        "keyword": "best 100%25 drama %2B apps",
        "location_code": 2840,
        "language_code": "en",
    }
    assert sent[(SERP, "free apps")] == {
        "keyword": "free apps",
        "location_code": 2840,
        "language_code": "en",
        "depth": 10,
        "load_async_ai_overview": True,
    }
    found = await service.results(user, project_id)
    run = found["run"]
    assert (run["status"], run["answers_done"], run["answers_failed"]) == ("completed", 9, 0)
    assert run["cost_usd"] == pytest.approx(0.036) and run["error"] is None
    # The branded prompt is left out by default; Gemini showed no answer.
    assert found["coverage"] == {"answered": 4, "no_answer": 2, "failed": 0}
    rows = {(row["prompt"], row["engine"]): row for row in found["rows"]}
    assert len(rows) == 6 and ("example.com reviews", "chatgpt") not in rows
    best = rows[("best 100% drama + apps", "chatgpt")]
    assert best["status"] == "answered" and best["citation_count"] == 2
    assert [(b["domain"], b["mentioned"], b["cited"]) for b in best["brands"]] == [
        ("example.com", True, True),
        ("beta.io", False, False),
        ("rival.com", True, False),
    ]
    assert rows[("free apps", "gemini")]["status"] == "no_answer"
    summaries = {s["domain"]: s for s in found["summaries"]}
    assert [s["domain"] for s in found["summaries"]] == ["example.com", "beta.io", "rival.com"]
    # Answered rows only: two ChatGPT answers and two AI Overviews.
    assert (summaries["example.com"]["answers"], summaries["example.com"]["mentions"]) == (4, 3)
    assert summaries["example.com"]["citations"] == 3
    assert summaries["example.com"]["avg_position"] == 1.33
    assert (summaries["rival.com"]["mentions"], summaries["rival.com"]["avg_position"]) == (2, 1)
    assert summaries["beta.io"]["mentions"] == 1
    assert found["sources"][0] == {
        "domain": "example.com",
        "citations": 3,
        "answers": 3,
        "owner": "own",
    }
    assert {s["domain"]: s["owner"] for s in found["sources"]} == {
        "example.com": "own",
        "rival.com": "competitor",
        "news.com": None,
    }
    branded = await service.results(user, project_id, branded="branded")
    assert {row["prompt"] for row in branded["rows"]} == {"example.com reviews"}
    assert len((await service.results(user, project_id, branded="all"))["rows"]) == 9
    gap = await service.results(user, project_id, gap=True)
    assert [(row["prompt"], row["engine"]) for row in gap["rows"]] == [("free apps", "chatgpt")]
    detail = await service.answer(user, project_id, run["run_id"], best["prompt_id"], "chatgpt")
    assert detail["answer_markdown"].startswith("Rival leads")
    assert [(c["domain"], c["owner"]) for c in detail["citations"]] == [
        ("example.com", "own"),
        ("news.com", None),
    ]
    with pytest.raises(OctopError) as missing:
        await service.answer(user, project_id, run["run_id"], best["prompt_id"], "claude")
    assert missing.value.code == "NOT_FOUND"
    # The same key is the same run, and nothing more is bought.
    again = await service.start_check(user, project_id, **check("k1"))
    assert again["run_id"] == started["run_id"] and len(bodies) == 9
    # An estimate above what was approved buys nothing.
    with pytest.raises(OctopError) as dearer:
        await service.start_check(user, project_id, **check("k2", max_cost_usd=0.01))
    assert dearer.value.code == "SEO_CONFLICT" and len(bodies) == 9
    # Removing a prompt or a competitor later keeps the saved answers as they were.
    await service.remove_prompt(user, project_id, best["prompt_id"])
    await context[0].update_context(
        user, project_id, [{"remove_competitors": ["rival.com"]}], author="user"
    )
    kept = await service.results(user, project_id, run_id=run["run_id"])
    assert len(kept["rows"]) == 6 and len(kept["run"]["brands"]) == 3
    assert [b["domain"] for b in kept["brands"]] == ["example.com", "beta.io"]


async def test_failures_busy_projects_and_interrupted_runs(context, monkeypatch, caplog):
    gate = asyncio.Event()
    replies = []

    async def handler(request):
        await gate.wait()
        if replies:
            answer = replies.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return reply({"markdown": "Example"})

    service, project_id = await setup(context, monkeypatch, handler)
    _, repos, user, _ = context
    await service.set_engines(user, project_id, engines=["chatgpt"])
    await service.add_prompts(user, project_id, prompts=["a", "b", "c"])
    replies.extend([reply(None, cost=0, task_status=40501), httpx.ConnectError("down")])
    running = await service.start_check(user, project_id, **check("k1"))
    # One check at a time per project.
    with pytest.raises(OctopError) as busy:
        await service.start_check(user, project_id, **check("k2"))
    assert busy.value.code == "SEO_CONFLICT"
    gate.set()
    await drained()
    found = await service.results(user, project_id)
    run = found["run"]
    assert run["run_id"] == running["run_id"]
    # A rejected answer and a lost reply are failed rows; the lost one leaves the cost unknown.
    assert (run["status"], run["answers_done"], run["answers_failed"]) == ("completed", 3, 2)
    assert (run["cost_usd"], run["cost_known_usd"]) == (None, pytest.approx(0.004))
    assert found["coverage"] == {"answered": 1, "no_answer": 0, "failed": 2}
    failed = [row for row in found["rows"] if row["status"] == "failed"]
    assert {row["error"] for row in failed} == {"provider_rejected", "transport_unknown"}
    assert all(row["brands"] == [] for row in failed)
    # The supplier's task status is logged, so a refused endpoint can be told apart.
    assert "provider_rejected" in caplog.text and "40501" in caplog.text
    # A failed answer is not the brand being absent.
    assert found["summaries"][0]["answers"] == 1
    # Every answer failing fails the run.
    replies.extend([reply(None, cost=0, task_status=40501)] * 3)
    await service.start_check(user, project_id, **check("k3"))
    await drained()
    run = (await service.results(user, project_id))["run"]
    assert (run["status"], run["answers_failed"], run["cost_usd"]) == ("failed", 3, 0)
    assert [r["run_id"] for r in (await service.results(user, project_id))["runs"]][1] == (
        running["run_id"]
    )
    # A run whose heartbeat stopped (a restart) is settled as interrupted and frees the project.
    stale = {
        "run_id": "stale",
        "project_id": project_id,
        "idempotency_key": "old",
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "heartbeat_at": 0,
        "brands": [],
        "cost_usd": 0.004,
    }
    repos.seo_ai_repo.start_run(stale, 0, "")
    run = (await service.results(user, project_id))["run"]
    assert (run["run_id"], run["status"], run["error"], run["cost_usd"]) == (
        "stale",
        "unknown",
        "interrupted",
        None,
    )
    # Cancelling a running check (shutdown) closes it as interrupted with an unknown cost.
    gate.clear()
    await service.start_check(user, project_id, **check("k4"))
    await asyncio.sleep(0.05)
    for task in list(ai._CHECKS):
        task.cancel()
    await asyncio.gather(*list(ai._CHECKS), return_exceptions=True)
    run = (await service.results(user, project_id))["run"]
    assert (run["status"], run["error"], run["cost_usd"]) == ("unknown", "interrupted", None)


async def test_a_check_needs_prompts_a_provider_and_confirmation(context, monkeypatch):
    calls = []
    service, project_id = await setup(
        context, monkeypatch, lambda request: calls.append(1) or reply({})
    )
    _, repos, user, other = context
    with pytest.raises(OctopError) as empty:
        await service.start_check(user, project_id, **check("a"))
    assert empty.value.code == "SEO_INVALID_INPUT"
    await service.add_prompts(user, project_id, prompts=["x"])
    with pytest.raises(OctopError):
        await service.start_check(user, project_id, **check("b", confirm_paid=False))
    with pytest.raises(OctopError) as forbidden:
        await service.start_check(other, project_id, **check("c"))
    assert forbidden.value.code == "FORBIDDEN"
    with pytest.raises(OctopError) as missing:
        await service.results(user, project_id, run_id="nope")
    assert missing.value.code == "NOT_FOUND"
    repos.settings_repo.delete(f"seo.provider.{user}")
    with pytest.raises(OctopError) as provider:
        await service.start_check(user, project_id, **check("d"))
    assert provider.value.code == "SEO_PROVIDER_REQUIRED"
    assert calls == []
