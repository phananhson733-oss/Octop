"""Evidence-backed reports and issue follow-through, without provider or model calls."""

import json

import pytest

from octop.infra.errors import OctopError
from octop.infra.seo.audit.service import SeoAuditService
from octop.infra.seo.workflow import SeoWorkflow


async def audit_fixture(context, key="audit", complete=True, issue=True):
    seo, repos, user, _ = context
    projects = await seo.list_projects(user)
    project = (
        projects[0]
        if projects
        else await seo.create_project(user, name="Site", domain="example.com", agent_id="seo")
    )
    pid = project["project_id"]
    audit = await SeoAuditService(seo).start(user, pid, idempotency_key=key)
    with repos.seo_repo._db.transaction() as conn:
        conn.execute(
            "UPDATE seo_audits SET status='completed',crawl_complete=?,"
            "completed_at='2099-01-01T00:00:00+00:00' WHERE audit_id=?",
            (int(complete), audit["audit_id"]),
        )
        conn.execute(
            "INSERT INTO seo_audit_pages(audit_id,url,status_code,fetch_class,is_html,"
            "crawled_at) VALUES(?,? ,200,'ok',1,'2099-01-01T00:00:00+00:00')",
            (audit["audit_id"], "https://example.com/"),
        )
        if not issue:
            conn.execute(
                "UPDATE seo_audit_pages SET title=?,meta_description=?,h1_count=1,is_indexable=1 WHERE audit_id=?",
                ("A valid page title", "A valid description. " * 5, audit["audit_id"]),
            )
        if issue:
            conn.execute(
                "INSERT INTO seo_audit_issues(audit_id,issue_type,severity,page_url) "
                "VALUES(?,'missing-title','critical','https://example.com/')",
                (audit["audit_id"],),
            )
    return pid, audit["audit_id"]


async def test_audit_report_without_keyword_research_freezes_evidence(context):
    seo, repos, user, other = context
    pid, aid = await audit_fixture(context)
    workflow = SeoWorkflow(seo)
    bundle = await workflow.prepare(
        user, pid, report_type="technical-audit", sources=[{"kind": "audit", "id": aid}]
    )
    assert bundle["sources"][0]["data"]["issues"][0]["issue_type"] == "missing-title"
    with pytest.raises(OctopError):
        await workflow.bundle(other, pid, bundle["context_id"])
    await SeoAuditService(seo).delete(user, pid, aid)
    assert (await workflow.bundle(user, pid, bundle["context_id"])) == bundle
    report = await seo.save_report(
        user,
        pid,
        context_id=bundle["context_id"],
        context_hash=bundle["context_hash"],
        title="Audit",
        summary="Measured audit",
        html="<html>Audit</html>",
    )
    assert report["run_id"] is None
    assert report["report_type"] == "technical-audit"
    assert (await seo.get_report(user, pid, report["report_id"]))["html"] == "<html>Audit</html>"
    with repos.seo_repo._db.connect() as conn:
        frozen = json.loads(
            conn.execute(
                "SELECT context_json FROM seo_reports WHERE report_id=?", (report["report_id"],)
            ).fetchone()["context_json"]
        )
    assert frozen == bundle
    with pytest.raises(OctopError):
        await seo.save_report(
            user,
            pid,
            context_id=bundle["context_id"],
            context_hash="0" * 64,
            title="Bad",
            summary="",
            html="<html>Bad</html>",
        )


async def test_evidence_rejects_foreign_and_running_audits(context):
    seo, repos, user, _ = context
    pid, aid = await audit_fixture(context)
    repos.agent_repo.create(agent_id="second", user_id=user, name="Other")
    other = await seo.create_project(user, name="Other", domain="other.example", agent_id="second")
    workflow = SeoWorkflow(seo)
    with pytest.raises(OctopError):
        await workflow.prepare(
            user,
            other["project_id"],
            report_type="technical-audit",
            sources=[{"kind": "audit", "id": aid}],
        )
    with repos.seo_repo._db.transaction() as conn:
        conn.execute("UPDATE seo_audits SET status='running' WHERE audit_id=?", (aid,))
    with pytest.raises(OctopError):
        await workflow.prepare(
            user, pid, report_type="technical-audit", sources=[{"kind": "audit", "id": aid}]
        )


async def test_action_requires_implementation_and_complete_fresh_recheck(context):
    seo, repos, user, _ = context
    pid, aid = await audit_fixture(context)
    workflow = SeoWorkflow(seo)
    action = await workflow.create_action(
        user, pid, audit_id=aid, issue_type="missing-title", page_url="https://example.com/"
    )
    again = await workflow.create_action(
        user, pid, audit_id=aid, issue_type="missing-title", page_url="https://example.com/"
    )
    assert again["action_id"] == action["action_id"]
    assert action["source"]["labels"]["zh"] != "missing-title"
    with pytest.raises(OctopError):
        await workflow.update_action(user, pid, action["action_id"], status="implemented", note="")
    await workflow.update_action(
        user, pid, action["action_id"], status="implemented", note="Title added; change abc123"
    )
    _, partial = await audit_fixture(context, "partial", complete=False, issue=False)
    result = await workflow.verify_action(user, pid, action["action_id"], audit_id=partial)
    assert result["status"] == "needs_review"
    _, clean = await audit_fixture(context, "clean", issue=False)
    result = await workflow.verify_action(user, pid, action["action_id"], audit_id=clean)
    assert result["status"] == "verified"
    assert result["verification"]["audit_id"] == clean
    assert result["history"][-1]["status"] == "verified"


@pytest.mark.parametrize(
    "column,value",
    [
        ("javascript_shell", 1),
        ("body_truncated", 1),
        ("status_code", 503),
        ("fetch_class", "blocked"),
    ],
)
async def test_unobservable_page_cannot_verify(context, column, value):
    seo, repos, user, _ = context
    pid, aid = await audit_fixture(context)
    workflow = SeoWorkflow(seo)
    action = await workflow.create_action(
        user, pid, audit_id=aid, issue_type="missing-title", page_url="https://example.com/"
    )
    await workflow.update_action(
        user, pid, action["action_id"], status="implemented", note="change"
    )
    _, later = await audit_fixture(context, "later", issue=False)
    with repos.seo_repo._db.transaction() as conn:
        conn.execute(f"UPDATE seo_audit_pages SET {column}=? WHERE audit_id=?", (value, later))
    assert (await workflow.verify_action(user, pid, action["action_id"], audit_id=later))[
        "status"
    ] == "needs_review"


async def test_shared_expert_can_save_evidence_report(context, monkeypatch):
    from octop.infra.seo.tools import build_seo_tools

    seo, repos, user, other = context
    pid, aid = await audit_fixture(context)
    repos.agent_repo.update_config("seo", published_expert_id="shared-seo")
    repos.user_repo.set_permissions(other, ["seo"])
    repos.agent_repo.create(
        agent_id="member-seo", user_id=other, name="SEO", published_expert_id="shared-seo"
    )
    repos.thread_repo.insert(
        thread_id="member-thread",
        agent_id="member-seo",
        user_id=other,
        channel_type="web",
        session_key="member-test",
    )
    thread = repos.thread_repo.get("member-thread")
    monkeypatch.setattr(
        "octop.infra.seo.tools.get_config",
        lambda: {
            "configurable": {
                "user": str(other),
                "agent_id": "member-seo",
                "thread_id": thread.thread_id,
            }
        },
    )
    tools = {t.name: t for t in build_seo_tools(seo, "member-seo", repos.thread_repo)}
    bundle = json.loads(
        await tools["seo_prepare_report"].ainvoke(
            {"report_type": "technical-audit", "sources": [{"kind": "audit", "id": aid}]}
        )
    )
    result = json.loads(
        await tools["seo_save_evidence_report"].ainvoke(
            {
                "context_id": bundle["context_id"],
                "context_hash": bundle["context_hash"],
                "title": "Audit",
                "summary": "",
                "html": "<html>Shared report</html>",
            }
        )
    )
    assert result["thread_id"] == thread.thread_id
    action = json.loads(
        await tools["seo_create_action"].ainvoke(
            {"audit_id": aid, "issue_type": "missing-title", "page_url": "https://example.com/"}
        )
    )
    await tools["seo_update_action"].ainvoke(
        {"action_id": action["action_id"], "status": "implemented", "note": "Member added title"}
    )
    _, later = await audit_fixture(context, "shared-recheck", issue=False)
    await tools["seo_verify_action"].ainvoke({"action_id": action["action_id"], "audit_id": later})
    history = next(a for a in repos.seo_repo.actions(pid) if a["action_id"] == action["action_id"])[
        "history"
    ]
    assert [event["by"] for event in history] == [other, other, other]
    assert (
        "actor_id" not in tools["seo_create_action"].args_schema.model_json_schema()["properties"]
    )

    repos.user_repo.set_permissions(other, [])
    with pytest.raises(OctopError):
        await tools["seo_read_report_evidence"].ainvoke({"context_id": bundle["context_id"]})


async def test_bundle_validation_integrity_and_size(context, monkeypatch):
    from octop.infra.seo import workflow as module

    seo, repos, user, _ = context
    pid, aid = await audit_fixture(context)
    workflow = SeoWorkflow(seo)
    for args in [
        {"report_type": "technical-audit", "sources": []},
        {"report_type": "search-performance", "sources": [{"kind": "audit", "id": aid}]},
        {
            "report_type": "technical-audit",
            "sources": [{"kind": "audit", "id": aid, "query": {"bad": True}}],
        },
    ]:
        with pytest.raises(OctopError):
            await workflow.prepare(user, pid, **args)
    with pytest.raises(OctopError):
        await workflow.bundle(user, pid, "absent")
    bundle = await workflow.prepare(
        user, pid, report_type="technical-audit", sources=[{"kind": "audit", "id": aid}]
    )
    bundle["sources"][0]["data"]["issues"] = []
    with repos.seo_repo._db.transaction() as conn:
        conn.execute(
            "UPDATE seo_report_contexts SET payload=? WHERE context_id=?",
            (json.dumps(bundle), bundle["context_id"]),
        )
    with pytest.raises(OctopError):
        await workflow.bundle(user, pid, bundle["context_id"])
    monkeypatch.setattr(module, "MAX_EVIDENCE_BYTES", 10)
    with pytest.raises(OctopError):
        await workflow.prepare(
            user, pid, report_type="technical-audit", sources=[{"kind": "audit", "id": aid}]
        )


async def test_live_google_queries_freeze_once(context, monkeypatch):
    from unittest.mock import AsyncMock

    from octop.infra.seo.google.service import SeoGoogleService

    seo, repos, user, _ = context
    pid, _ = await audit_fixture(context)
    gsc = AsyncMock(
        return_value={
            "request": {"startDate": "2026-09-01", "endDate": "2026-09-30"},
            "rows": [],
            "fetched": 0,
        }
    )
    ga4 = AsyncMock(
        return_value={"source": {"property_id": "properties/1"}, "rows": [], "row_count": 0}
    )
    monkeypatch.setattr(SeoGoogleService, "gsc_performance", gsc)
    monkeypatch.setattr(SeoGoogleService, "ga4_report", ga4)
    workflow = SeoWorkflow(seo)
    bundle = await workflow.prepare(
        user,
        pid,
        report_type="search-performance",
        sources=[
            {"kind": "gsc", "query": {"dimensions": ["page", "query"]}},
            {"kind": "ga4", "query": {"kind": "landing_pages"}},
        ],
    )
    assert bundle["sources"][0]["data"]["request"]["startDate"] == "2026-09-01"
    gsc.return_value = {"rows": ["changed"]}
    assert (await workflow.bundle(user, pid, bundle["context_id"])) == bundle
    assert gsc.await_count == ga4.await_count == 1
    for source in [
        {"kind": "gsc", "id": "invalid"},
        {"kind": "ga4", "query": {"kind": "unsupported"}},
    ]:
        with pytest.raises(OctopError):
            await workflow.prepare(user, pid, report_type="search-performance", sources=[source])


async def test_action_needs_review_for_missing_issues_and_old_observations(context):
    seo, repos, user, _ = context
    pid, aid = await audit_fixture(context)
    workflow = SeoWorkflow(seo)
    with pytest.raises(OctopError):
        await workflow.create_action(
            user, pid, audit_id=aid, issue_type="invented", page_url="https://example.com/"
        )
    action = await workflow.create_action(
        user, pid, audit_id=aid, issue_type="missing-title", page_url="https://example.com/"
    )
    with pytest.raises(OctopError):
        await workflow.verify_action(user, pid, action["action_id"], audit_id=aid)
    await workflow.update_action(
        user, pid, action["action_id"], status="implemented", note="change"
    )
    assert (await workflow.verify_action(user, pid, action["action_id"], audit_id=aid))[
        "verification"
    ]["reason"] == "issue_present"
    _, later = await audit_fixture(context, "old", issue=False)
    with repos.seo_repo._db.transaction() as conn:
        conn.execute(
            "UPDATE seo_audit_pages SET crawled_at='2000-01-01T00:00:00+00:00' WHERE audit_id=?",
            (later,),
        )
    assert (await workflow.verify_action(user, pid, action["action_id"], audit_id=later))[
        "status"
    ] == "needs_review"
    reopened = await workflow.update_action(
        user, pid, action["action_id"], status="open", note="revisit"
    )
    assert reopened["implementation"] is None and reopened["verification"] is None
    assert len(reopened["history"]) == 5


@pytest.mark.parametrize(
    "issue_type",
    [
        "thin-content",
        "no-outgoing-links",
        "deep-page",
        "orphan-page",
        "not-in-sitemap",
        "render-title-changed",
    ],
)
async def test_conditional_rules_require_manual_review(context, issue_type):
    seo, repos, user, _ = context
    pid, aid = await audit_fixture(context)
    with repos.seo_repo._db.transaction() as conn:
        conn.execute("UPDATE seo_audit_issues SET issue_type=? WHERE audit_id=?", (issue_type, aid))
    workflow = SeoWorkflow(seo)
    action = await workflow.create_action(
        user, pid, audit_id=aid, issue_type=issue_type, page_url="https://example.com/"
    )
    await workflow.update_action(
        user, pid, action["action_id"], status="implemented", note="change"
    )
    _, later = await audit_fixture(context, "later", issue=False)
    result = await workflow.verify_action(user, pid, action["action_id"], audit_id=later)
    assert result["status"] == "needs_review"
    assert result["verification"]["reason"] == "manual_review_required"


@pytest.mark.parametrize(
    "issue_type,field,value",
    [
        ("title-too-long", "title", ""),
        ("title-too-short", "title", "x" * 80),
        ("missing-title", "title", "x"),
        ("meta-description-too-long", "meta_description", ""),
        ("meta-description-too-short", "meta_description", "x" * 200),
        ("missing-meta-description", "meta_description", "x"),
        ("multiple-h1", "h1_count", 0),
        ("missing-h1", "h1_count", 2),
        ("heading-order-skip", "h1_count", 0),
    ],
)
async def test_replacement_errors_are_not_verified(context, issue_type, field, value):
    from octop.infra.seo.audit.rules import page_issues
    from octop.infra.seo.audit.runner import facts_from_row

    seo, repos, user, _ = context
    pid, aid = await audit_fixture(context)
    with repos.seo_repo._db.transaction() as conn:
        conn.execute("UPDATE seo_audit_issues SET issue_type=? WHERE audit_id=?", (issue_type, aid))
    workflow = SeoWorkflow(seo)
    action = await workflow.create_action(
        user, pid, audit_id=aid, issue_type=issue_type, page_url="https://example.com/"
    )
    await workflow.update_action(
        user, pid, action["action_id"], status="implemented", note="Changed target field"
    )
    _, later = await audit_fixture(context, "replacement", issue=False)
    with repos.seo_repo._db.transaction() as conn:
        conn.execute(f"UPDATE seo_audit_pages SET {field}=? WHERE audit_id=?", (value, later))
    current = page_issues(facts_from_row(repos.seo_audit_repo.pages(later)[0]))
    with repos.seo_repo._db.transaction() as conn:
        for issue in current:
            conn.execute(
                "INSERT INTO seo_audit_issues(audit_id,issue_type,severity,page_url,details) VALUES(?,?,?,?,?)",
                (
                    later,
                    issue.issue_type,
                    issue.severity,
                    issue.page_url,
                    json.dumps(issue.details),
                ),
            )
    result = await workflow.verify_action(user, pid, action["action_id"], audit_id=later)
    assert result["status"] == "needs_review"
    assert result["verification"]["reason"] == "target_not_satisfied"


async def test_comprehensive_report_freezes_selected_keywords_and_tags(context, monkeypatch):
    from octop.infra.seo.tools import build_seo_tools

    seo, repos, user, _ = context
    pid, aid = await audit_fixture(context)
    row = {
        "keyword": "selected-word",
        "search_volume": 10,
        "cpc": None,
        "competition": None,
        "keyword_difficulty": None,
        "intent": "unknown",
        "trend": [],
        "volume_source": "keyword_info",
    }
    run = {
        "run_id": "shortlist-run",
        "project_id": pid,
        "status": "completed",
        "observed_at": "2026-10-29T00:00:00+00:00",
        "location_code": 2840,
        "language_code": "en",
        "results": [
            {"seed": "seed", "ok": True, "source": "ideas", "used_fallback": False, "rows": [row]}
        ],
    }
    repos.seo_repo.start_run(run, "shortlist", "hash")
    await seo.save_keywords(
        user, pid, run_id=run["run_id"], keywords=["selected-word"], tags=["priority-marker"]
    )
    bundle = await SeoWorkflow(seo).prepare(
        user,
        pid,
        report_type="comprehensive",
        sources=[{"kind": "research", "id": run["run_id"]}, {"kind": "audit", "id": aid}],
    )
    selected = bundle["saved_keywords"][0]
    assert selected["keyword"] == "selected-word" and selected["tags"] == ["priority-marker"]
    assert (
        selected["observed_at"] == run["observed_at"] and selected["source_run_id"] == run["run_id"]
    )
    await seo.save_keywords(
        user,
        pid,
        run_id=run["run_id"],
        keywords=["selected-word"],
        tags=["changed"],
        tag_mode="replace",
    )
    assert (await SeoWorkflow(seo).bundle(user, pid, bundle["context_id"]))["saved_keywords"][0][
        "tags"
    ] == ["priority-marker"]
    repos.thread_repo.insert(
        thread_id="shortlist-thread",
        agent_id="seo",
        user_id=user,
        channel_type="web",
        session_key="shortlist",
    )
    monkeypatch.setattr(
        "octop.infra.seo.tools.get_config",
        lambda: {
            "configurable": {"user": str(user), "agent_id": "seo", "thread_id": "shortlist-thread"}
        },
    )
    tools = {t.name: t for t in build_seo_tools(seo, "seo", repos.thread_repo)}
    frozen = json.loads(
        await tools["seo_read_report_evidence"].ainvoke(
            {"context_id": bundle["context_id"], "source_index": -2}
        )
    )
    assert "priority-marker" in frozen["text"] and "changed" not in frozen["text"]
    saved = await seo.save_report(
        user,
        pid,
        context_id=bundle["context_id"],
        context_hash=bundle["context_hash"],
        title="Selected strategy",
        summary="",
        html="<html>Selected strategy</html>",
    )
    with repos.seo_repo._db.connect() as conn:
        stored = json.loads(
            conn.execute(
                "SELECT context_json FROM seo_reports WHERE report_id=?", (saved["report_id"],)
            ).fetchone()["context_json"]
        )
    assert stored["saved_keywords"] == bundle["saved_keywords"]
    # Historical bundles stay historical; no live shortlist is substituted on read.
    from octop.infra.seo.workflow import _digest

    legacy = {k: v for k, v in bundle.items() if k not in {"saved_keywords", "context_hash"}}
    legacy["context_id"] = "legacy-bundle"
    legacy["context_hash"] = _digest(legacy)
    repos.seo_repo.save_context(legacy)
    old = json.loads(
        await tools["seo_read_report_evidence"].ainvoke(
            {"context_id": "legacy-bundle", "source_index": -2}
        )
    )
    assert old["text"] == "null"
