from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import RepoBundle
from octop.infra.errors import OctopError
from octop.infra.seo.service import SeoService


@pytest.fixture
def context(tmp_path: Path):
    pool = SqlitePool(tmp_path / "seo.db")
    run_migrations(pool)
    repos = RepoBundle.from_pool(pool)
    user = repos.user_repo.create(username="alice", role="user")
    other = repos.user_repo.create(username="bob", role="user")
    repos.agent_repo.create(agent_id="seo", user_id=user, name="SEO")
    yield SeoService(repos), repos, user, other
    pool.close()


async def test_project_owner_and_agent_unique(context):
    service, repos, user, other = context
    project = await service.create_project(
        user, name="Example", domain="example.com", agent_id="seo"
    )
    assert project["location_code"] == 2840
    assert await service.list_projects(other) == []
    assert (await service.project_for_agent(user, "seo"))["project_id"] == project["project_id"]
    assert await service.project_for_agent(other, "seo") is None
    with pytest.raises(OctopError):
        await service.create_project(other, name="Stolen", domain="example.org", agent_id="seo")
    with pytest.raises(OctopError):
        await service.create_project(user, name="Duplicate", domain="example.org", agent_id="seo")


async def test_provider_encrypted_and_status_only(context):
    service, repos, user, other = context
    assert await service.provider_status(user) == {"configured": False}
    await service.set_provider(user, login="test-login", password="super-secret")
    assert await service.provider_status(user) == {"configured": True}
    assert await service.provider_status(other) == {"configured": False}
    assert "super-secret" not in repos.settings_repo.get(f"seo.provider.{user}")


async def configured(context, monkeypatch, handler):
    import functools

    import httpx

    import octop.infra.seo.service as module
    from octop.infra.seo.provider import DataForSeoClient

    service, repos, user, other = context
    monkeypatch.setattr(
        module,
        "DataForSeoClient",
        functools.partial(DataForSeoClient, transport=httpx.MockTransport(handler)),
    )
    project = await service.create_project(
        user, name="Example", domain="example.com", agent_id="seo"
    )
    await service.set_provider(user, login="test", password="secret")
    return project["project_id"]


def response(items, *, cost=0.01, task_status=20000):
    import httpx

    return httpx.Response(
        200,
        json={
            "status_code": 20000,
            "tasks": [
                {
                    "id": "fixture-task",
                    "status_code": task_status,
                    "cost": cost,
                    "result": [{"items": items}],
                }
            ],
        },
    )


async def test_research_save_report_roundtrip(context, monkeypatch):
    import json

    calls = []

    def handler(request):
        calls.append(json.loads(request.content)[0])
        words = ["caregiving", "a", "b"] if "suggestions" in request.url.path else ["c", "d", "e"]
        return response(
            [
                {"keyword": k, "keyword_info": {"search_volume": 0 if k == "a" else 100}}
                for k in words
            ]
        )

    project_id = await configured(context, monkeypatch, handler)
    service, repos, user, other = context
    command = {
        "keywords": [" Caregiving ", "caregiving"],
        "mode": "auto",
        "result_limit": 150,
        "idempotency_key": "first",
        "confirm_paid": True,
    }
    run = await service.research(user, project_id, **command)
    assert run["status"] == "completed" and run["cost_usd"] == 0.02
    assert len(calls) == 2 and all(c["limit"] == 75 for c in calls)
    assert [r["keyword"] for r in run["results"][0]["rows"]] == [
        "caregiving",
        "c",
        "a",
        "d",
        "b",
        "e",
    ]
    assert (await service.research(user, project_id, **command))["run_id"] == run["run_id"]
    cached = await service.research(user, project_id, **(command | {"idempotency_key": "cached"}))
    assert cached["cached"] and cached["cost_usd"] == 0 and len(calls) == 2
    assert cached["observed_at"] == run["observed_at"]
    with pytest.raises(OctopError) as conflict:
        await service.research(user, project_id, **(command | {"keywords": ["different"]}))
    assert conflict.value.status == 409
    counts = await service.save_keywords(
        user,
        project_id,
        run_id=run["run_id"],
        keywords=[" A ", "a", "caregiving"],
        tags=["priority"],
    )
    assert counts == {"created_count": 2, "updated_count": 0, "saved_count": 2}
    first = (await service.list_keywords(user, project_id))["rows"][0]
    assert first["search_volume"] == 0 and first["observed_at"] == run["observed_at"]
    await service.save_keywords(
        user, project_id, run_id=run["run_id"], keywords=["a"], tags=["second"]
    )
    row = (await service.list_keywords(user, project_id))["rows"][0]
    assert row["keyword_id"] == first["keyword_id"] and row["tags"] == ["priority", "second"]
    await service.save_keywords(
        user, project_id, run_id=run["run_id"], keywords=["a"], tags=["replace"], tag_mode="replace"
    )
    assert (await service.list_keywords(user, project_id))["rows"][0]["tags"] == ["replace"]
    for invalid in [
        {"keywords": ["forged"]},
        {"keywords": ["a"], "tag_mode": "replace", "tags": []},
        {"keywords": ["a"], "metrics": [{"search_volume": 999}]},
    ]:
        with pytest.raises(OctopError):
            await service.save_keywords(user, project_id, run_id=run["run_id"], **invalid)
    with pytest.raises(OctopError):
        await service.get_run(other, project_id, run["run_id"])
    context_hash = (await service.report_context(user, project_id, run["run_id"]))["context_hash"]
    html = "<html><body>证据报告</body></html>"
    report = await service.save_report(
        user,
        project_id,
        run_id=run["run_id"],
        context_hash=context_hash,
        title="Evidence",
        summary="Offline transport fixture",
        html=html,
    )
    another = await service.save_report(
        user,
        project_id,
        run_id=run["run_id"],
        context_hash=context_hash,
        title="Evidence",
        summary="v2",
        html=html,
    )
    assert another["report_id"] != report["report_id"]
    assert (await service.get_report(user, project_id, report["report_id"]))["html"] == html
    assert all("html" not in item for item in await service.list_reports(user, project_id))
    with pytest.raises(OctopError):
        await service.get_report(other, project_id, report["report_id"])
    with pytest.raises(OctopError):
        await service.save_report(
            user,
            project_id,
            run_id=run["run_id"],
            context_hash=context_hash,
            title="Oversize",
            summary="",
            html="<html>" + ("字" * 170000) + "</html>",
        )
    with repos.db.transaction() as conn:
        conn.execute(
            "UPDATE seo_reports SET html=? WHERE report_id=?",
            ("<html>tampered</html>", report["report_id"]),
        )
    with pytest.raises(OctopError) as integrity:
        await service.get_report(user, project_id, report["report_id"])
    assert integrity.value.code.value == "SEO_REPORT_INTEGRITY"


async def test_charged_failure_and_partial_seeds(context, monkeypatch):
    import json

    def handler(request):
        seed = json.loads(request.content)[0]["keywords"][0]
        return response(
            [{"keyword": "ok"}],
            cost=0.04 if seed == "bad" else 0.01,
            task_status=40501 if seed == "bad" else 20000,
        )

    project_id = await configured(context, monkeypatch, handler)
    service, repos, user, other = context
    run = await service.research(
        user,
        project_id,
        keywords=["bad", "good"],
        mode="ideas",
        confirm_paid=True,
        idempotency_key="partial",
    )
    assert run["status"] == "partial" and run["cost_usd"] == 0.05
    assert run["results"][0]["ok"] is False and run["results"][1]["ok"] is True
    assert len(run["calls"]) == 2
    assert run["calls"][0]["receipt"]["task_id"] == "fixture-task"
    assert (await service.get_run(user, project_id, run["run_id"]))["cost_usd"] == 0.05


async def test_both_auto_legs_settle_and_preserve_known_cost(context, monkeypatch):
    import httpx

    calls = []

    def handler(request):
        calls.append(request.url.path)
        if "ideas" in request.url.path:
            raise httpx.ReadTimeout("unknown charge")
        return response([{"keyword": "seed"}], cost=0.012)

    project_id = await configured(context, monkeypatch, handler)
    service, repos, user, other = context
    run = await service.research(
        user,
        project_id,
        keywords=["seed"],
        mode="auto",
        confirm_paid=True,
        idempotency_key="unknown",
    )
    assert run["status"] == "unknown" and run["cost_usd"] is None
    assert run["cost_known_usd"] == 0.012 and len(calls) == 2 and run["results"][0]["rows"] == []
    again = await service.research(
        user,
        project_id,
        keywords=["seed"],
        mode="auto",
        confirm_paid=True,
        idempotency_key="unknown",
    )
    assert again["run_id"] == run["run_id"] and len(calls) == 2


async def test_reference_fallback_threshold(context, monkeypatch):
    import json

    reference = json.loads(
        (Path(__file__).parents[2] / "fixtures/seo/openseo-reference.json").read_text()
    )
    case = reference["research"][1]
    calls = []

    def handler(request):
        source = next(s for s in ["suggestions", "ideas", "related"] if s in request.url.path)
        calls.append(source)
        items = [{"keyword": k} for k in case["providerItems"][source]]
        return response(
            [{"keyword_data": item} for item in items] if source == "related" else items
        )

    project_id = await configured(context, monkeypatch, handler)
    service, _, user, _ = context
    run = await service.research(
        user, project_id, keywords=["caregiving"], confirm_paid=True, idempotency_key="fallback"
    )
    assert run["results"][0]["used_fallback"]
    assert [row["keyword"] for row in run["results"][0]["rows"]] == case["expected"]["keywords"]
    assert calls == ["suggestions", "ideas", "related"]


async def test_saved_batch_rolls_back_on_database_failure(context, monkeypatch):
    project_id = await configured(
        context, monkeypatch, lambda request: response([{"keyword": "a"}, {"keyword": "b"}])
    )
    service, repos, user, _ = context
    run = await service.research(
        user,
        project_id,
        keywords=["seed"],
        mode="ideas",
        confirm_paid=True,
        idempotency_key="atomic",
    )
    with repos.db.transaction() as conn:
        conn.execute(
            "CREATE TRIGGER fail_keyword BEFORE INSERT ON seo_keywords WHEN NEW.keyword='b' BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    with pytest.raises(Exception, match="fixture"):
        await service.save_keywords(
            user, project_id, run_id=run["run_id"], keywords=["a", "b"], tags=["atomic"]
        )
    assert (await service.list_keywords(user, project_id))["total_count"] == 0


async def test_concurrent_idempotency_only_buys_once(context, monkeypatch):
    import asyncio

    calls = []

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(0.02)
        return response([{"keyword": "one"}])

    project_id = await configured(context, monkeypatch, handler)
    service, _, user, _ = context
    command = {
        "keywords": ["seed"],
        "mode": "ideas",
        "confirm_paid": True,
        "idempotency_key": "same",
    }
    first, second = await asyncio.gather(
        service.research(user, project_id, **command), service.research(user, project_id, **command)
    )
    assert first["run_id"] == second["run_id"]
    assert len(calls) == 1
    assert (await service.get_run(user, project_id, first["run_id"]))["status"] == "completed"


async def test_interrupted_run_unknown_and_expired_cache_not_extended(context, monkeypatch):
    from datetime import UTC, datetime, timedelta

    calls = []

    def handler(request):
        calls.append(request)
        return response([{"keyword": "one"}])

    project_id = await configured(context, monkeypatch, handler)
    service, repos, user, _ = context
    command = {
        "keywords": ["seed"],
        "mode": "ideas",
        "confirm_paid": True,
        "idempotency_key": "first",
    }
    run = await service.research(user, project_id, **command)
    run["observed_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    repos.seo_repo.update_run(run)
    second = await service.research(user, project_id, **(command | {"idempotency_key": "second"}))
    assert len(calls) == 2 and not second["cached"]
    second.update(
        status="running", observed_at=(datetime.now(UTC) - timedelta(minutes=21)).isoformat()
    )
    repos.seo_repo.update_run(second)
    recovered = await service.get_run(user, project_id, second["run_id"])
    assert recovered["status"] == "unknown" and recovered["error"] == "interrupted"
    await service.research(user, project_id, **(command | {"idempotency_key": "second"}))
    assert len(calls) == 2


async def test_concurrent_reversed_save_batches_preserve_tags(context, monkeypatch):
    import asyncio

    project_id = await configured(
        context, monkeypatch, lambda request: response([{"keyword": "a"}, {"keyword": "b"}])
    )
    service, _, user, _ = context
    run = await service.research(
        user, project_id, keywords=["seed"], mode="ideas", confirm_paid=True, idempotency_key="save"
    )
    await asyncio.gather(
        service.save_keywords(
            user, project_id, run_id=run["run_id"], keywords=["a", "b"], tags=["first"]
        ),
        service.save_keywords(
            user, project_id, run_id=run["run_id"], keywords=["b", "a"], tags=["second"]
        ),
    )
    rows = (await service.list_keywords(user, project_id))["rows"]
    assert len(rows) == 2 and all(row["tags"] == ["first", "second"] for row in rows)


async def test_report_thread_provenance_and_source_project_are_bound(context, monkeypatch):
    project_id = await configured(
        context, monkeypatch, lambda request: response([{"keyword": "a"}])
    )
    service, repos, user, other = context
    run = await service.research(
        user,
        project_id,
        keywords=["seed"],
        mode="ideas",
        confirm_paid=True,
        idempotency_key="provenance",
    )
    repos.thread_repo.insert(
        thread_id="owned",
        agent_id="seo",
        user_id=user,
        channel_type="dashboard",
        session_key="owned",
    )
    repos.thread_repo.insert(
        thread_id="other-user",
        agent_id="seo",
        user_id=other,
        channel_type="dashboard",
        session_key="other",
    )
    repos.agent_repo.create(agent_id="second-agent", user_id=user, name="Second")
    other_project = await service.create_project(
        user, name="Second", domain="second.example", agent_id="second-agent"
    )
    args = {
        "run_id": run["run_id"],
        "context_hash": (await service.report_context(user, project_id, run["run_id"]))[
            "context_hash"
        ],
        "title": "Evidence",
        "summary": "Fixture",
        "html": "<html>evidence</html>",
    }
    report = await service.save_report(user, project_id, **args, thread_id="owned")
    assert report["thread_id"] == "owned"
    with pytest.raises(OctopError):
        await service.save_report(user, project_id, **args, thread_id="other-user")
    with pytest.raises(OctopError):
        await service.save_report(user, other_project["project_id"], **args)
    with pytest.raises(OctopError):
        await service.save_keywords(
            user, other_project["project_id"], run_id=run["run_id"], keywords=["a"]
        )


async def test_tags_normalize_identity_and_preserve_first_display_name(context, monkeypatch):
    project_id = await configured(
        context, monkeypatch, lambda request: response([{"keyword": "a"}, {"keyword": "b"}])
    )
    service, _, user, _ = context
    run = await service.research(
        user, project_id, keywords=["seed"], mode="ideas", confirm_paid=True, idempotency_key="tags"
    )
    await service.save_keywords(
        user,
        project_id,
        run_id=run["run_id"],
        keywords=["a"],
        tags=[" High   Priority ", "high priority"],
    )
    assert (await service.list_keywords(user, project_id))["rows"][0]["tags"] == ["High Priority"]
    await service.save_keywords(
        user, project_id, run_id=run["run_id"], keywords=["a"], tags=["other"], tag_mode="replace"
    )
    await service.save_keywords(
        user,
        project_id,
        run_id=run["run_id"],
        keywords=["b"],
        tags=["HIGH PRIORITY"],
        tag_mode="replace",
    )
    rows = (await service.list_keywords(user, project_id))["rows"]
    assert rows[1]["tags"] == ["High Priority"]
    with pytest.raises(OctopError):
        await service.save_keywords(
            user, project_id, run_id=run["run_id"], keywords=["a"], tags=["x" * 65]
        )


async def test_report_rejects_shortlist_drift_and_persists_exact_context(context, monkeypatch):
    import json

    project_id = await configured(
        context, monkeypatch, lambda request: response([{"keyword": "a"}, {"keyword": "b"}])
    )
    service, repos, user, _ = context
    run = await service.research(
        user,
        project_id,
        keywords=["seed"],
        mode="ideas",
        confirm_paid=True,
        idempotency_key="context",
    )
    original = await service.report_context(user, project_id, run["run_id"])
    assert (await service.report_context(user, project_id, run["run_id"]))[
        "context_hash"
    ] == original["context_hash"]
    await service.save_keywords(user, project_id, run_id=run["run_id"], keywords=["a"], tags=[])
    args = {
        "run_id": run["run_id"],
        "title": "Evidence",
        "summary": "",
        "html": "<html>evidence</html>",
    }
    with pytest.raises(OctopError) as conflict:
        await service.save_report(user, project_id, context_hash=original["context_hash"], **args)
    assert conflict.value.status == 409 and await service.list_reports(user, project_id) == []
    updated = await service.report_context(user, project_id, run["run_id"])
    report = await service.save_report(
        user, project_id, context_hash=updated["context_hash"], **args
    )
    with repos.db.connect() as conn:
        stored = conn.execute(
            "SELECT context_json FROM seo_reports WHERE report_id=?", (report["report_id"],)
        ).fetchone()
    assert json.loads(stored["context_json"]) == updated


async def test_default_ungrouped_and_complete_trimmed_report(context, monkeypatch):
    import json

    calls = []

    def handler(request):
        calls.append(json.loads(request.content)[0])
        return response([{"keyword": "evidence"}])

    project_id = await configured(context, monkeypatch, handler)
    service, _, user, _ = context
    run = await service.research(
        user,
        project_id,
        keywords=["seed"],
        mode="ideas",
        confirm_paid=True,
        idempotency_key="default-grouping",
    )
    assert run["inputs"]["group_keywords"] is False
    assert calls[0]["ignore_synonyms"] is True
    evidence = await service.report_context(user, project_id, run["run_id"])
    args = {
        "run_id": run["run_id"],
        "context_hash": evidence["context_hash"],
        "title": "Evidence",
        "summary": "Offline fixture",
    }
    for html in ("<html>evidence</html>trailing text", "<html>evidence</html><script>x</script>"):
        with pytest.raises(OctopError):
            await service.save_report(user, project_id, **args, html=html)
    report = await service.save_report(user, project_id, **args, html=" \n<HTML>evidence</HTML>\n ")
    saved = await service.get_report(user, project_id, report["report_id"])
    assert saved["html"] == "<HTML>evidence</HTML>"
