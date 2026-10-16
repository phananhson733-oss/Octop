"""Audits run end to end, survive restarts, and stop when fenced."""

import asyncio
import time
from collections import Counter
from typing import Any

import httpx
import pytest

from octop.infra.errors import OctopError
from octop.infra.seo.audit import runner as runner_module
from octop.infra.seo.audit.runner import SeoAuditRunner
from octop.infra.seo.audit.service import SeoAuditService
from octop.infra.seo.audit.throttle import CrawlThrottle

from .fakesite import FakeSite, page, redirect

ORIGIN = "https://example.com"


class _Clock:
    now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def instant_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(
        runner_module, "CrawlThrottle", lambda: CrawlThrottle(clock.clock, clock.sleep)
    )


def _site(gate: asyncio.Event | None = None) -> FakeSite:
    async def gated_b(_request: httpx.Request) -> httpx.Response:
        if gate is not None:
            await gate.wait()
        return page("Bee", "/")

    sitemap = "".join(
        f"<url><loc>{ORIGIN}{path}</loc></url>" for path in ("/", "/a", "/orphan", "/private/x")
    )
    return FakeSite(
        {
            f"{ORIGIN}/robots.txt": httpx.Response(
                200, text=f"User-agent: *\nDisallow: /private\nSitemap: {ORIGIN}/sitemap.xml"
            ),
            f"{ORIGIN}/sitemap.xml": httpx.Response(
                200, headers={"content-type": "application/xml"}, text=f"<urlset>{sitemap}</urlset>"
            ),
            f"{ORIGIN}/": page("Home", "/a", "/b", "/old", "/private/y", "https://other.example/"),
            f"{ORIGIN}/a": page("Ay", "/", "/gone"),
            f"{ORIGIN}/b": gated_b,
            f"{ORIGIN}/old": redirect("/b"),
            f"{ORIGIN}/orphan": page("Orphan", "/"),
        }
    )


@pytest.fixture
async def project(context: Any) -> tuple[SeoAuditService, Any, int, str]:
    service, repos, user, _other = context
    created = await service.create_project(user, name="Site", domain="example.com", agent_id="seo")
    return SeoAuditService(service), repos, user, created["project_id"]


async def _start(project: Any, key: str = "k1", **kwargs: Any) -> dict[str, Any]:
    audits, _repos, user, project_id = project
    return await audits.start(user, project_id, idempotency_key=key, **kwargs)


async def _until(predicate: Any, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition never held"
        await asyncio.sleep(0.01)


async def test_audit_crawls_the_site_and_reports_issues(project: Any) -> None:
    audits, repos, user, project_id = project
    site = _site()
    audit = await _start(project)
    runner = SeoAuditRunner(repos, client_factory=site.client)
    runner.submit(audit["audit_id"])
    await runner.wait(audit["audit_id"])

    done = await audits.get(user, project_id, audit["audit_id"])
    assert (done["status"], done["phase"], done["crawl_complete"]) == (
        "completed",
        "finalize",
        True,
    )
    assert done["start_url"] == f"{ORIGIN}/"
    pages = {row["url"]: row for row in (await audits.pages(user, project_id))["pages"]}
    assert set(pages) == {f"{ORIGIN}/{path}" for path in ("", "a", "b", "old", "gone", "orphan")}
    assert pages[f"{ORIGIN}/old"]["redirect_url"] == f"{ORIGIN}/b"
    assert (pages[f"{ORIGIN}/"]["in_sitemap"], pages[f"{ORIGIN}/b"]["in_sitemap"]) == (True, False)
    assert (pages[f"{ORIGIN}/a"]["crawl_depth"], pages[f"{ORIGIN}/b"]["crawl_depth"]) == (1, 1)
    # robots.txt is obeyed for seeds and links alike.
    assert not any("/private" in request for request in site.requests if "GET" in request)

    result = await audits.issues(user, project_id, locale="zh")
    found = {(row["issue_type"], row["page_url"]) for row in result["issues"]}
    assert ("broken-internal-link", f"{ORIGIN}/a") in found
    assert ("broken-page", f"{ORIGIN}/gone") in found
    assert ("orphan-page", f"{ORIGIN}/orphan") in found
    summary = {row["issue_type"]: row for row in result["summary"]}
    assert summary["broken-internal-link"]["title"] == "站内死链"
    assert [row["severity"] for row in result["summary"]] == sorted(
        (row["severity"] for row in result["summary"]),
        key=["critical", "warning", "info"].index,
    )
    assert done["issue_counts"]["critical"] == 1


async def test_a_restarted_process_resumes_without_repeating_work(project: Any) -> None:
    audits, repos, user, project_id = project
    gate = asyncio.Event()
    site = _site(gate)
    audit = await _start(project)
    first = SeoAuditRunner(repos, client_factory=site.client)
    first.submit(audit["audit_id"])
    await _until(lambda: f"GET {ORIGIN}/b" in site.requests)
    await first.stop()

    paused = await audits.get(user, project_id, audit["audit_id"])
    assert (paused["status"], paused["phase"]) == ("running", "crawl")
    assert 0 < paused["pages_crawled"] < 6

    gate.set()
    second = SeoAuditRunner(repos, client_factory=site.client)
    second.submit(audit["audit_id"])
    await second.wait(audit["audit_id"])
    done = await audits.get(user, project_id, audit["audit_id"])
    assert done["status"] == "completed"
    assert done["pages_crawled"] == 6
    gets = Counter(request for request in site.requests if request.startswith("GET"))
    assert gets[f"GET {ORIGIN}/"] == 1
    issues = (await audits.issues(user, project_id))["issues"]
    pairs = Counter((row["issue_type"], row["page_url"]) for row in issues)
    assert max(pairs.values()) == 1


async def test_cancel_stops_after_the_current_page(project: Any) -> None:
    audits, repos, user, project_id = project
    gate = asyncio.Event()
    site = _site(gate)
    audit = await _start(project)
    runner = SeoAuditRunner(repos, client_factory=site.client)
    runner.submit(audit["audit_id"])
    await _until(lambda: f"GET {ORIGIN}/b" in site.requests)
    cancelling = await audits.cancel(user, project_id, audit["audit_id"])
    assert (cancelling["status"], cancelling["cancel_requested"]) == ("running", True)
    gate.set()
    await runner.wait(audit["audit_id"])

    done = await audits.get(user, project_id, audit["audit_id"])
    assert done["status"] == "cancelled"
    pages = (await audits.pages(user, project_id))["pages"]
    assert f"{ORIGIN}/b" not in {row["url"] for row in pages}
    assert pages


async def test_deleting_a_running_audit_leaves_nothing_behind(project: Any) -> None:
    audits, repos, user, project_id = project
    gate = asyncio.Event()
    site = _site(gate)
    audit = await _start(project)
    runner = SeoAuditRunner(repos, client_factory=site.client)
    runner.submit(audit["audit_id"])
    await _until(lambda: f"GET {ORIGIN}/b" in site.requests)
    await audits.delete(user, project_id, audit["audit_id"])
    gate.set()
    await runner.wait(audit["audit_id"])
    with repos.seo_audit_repo._db.connect() as conn:
        for table in ("seo_audits", "seo_audit_frontier", "seo_audit_pages", "seo_audit_issues"):
            assert conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == 0


async def test_a_lost_claim_stops_the_job_without_cancelling_the_audit(
    project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    audits, repos, user, project_id = project
    monkeypatch.setattr(runner_module, "BEAT_S", 0.01)
    gate = asyncio.Event()
    site = _site(gate)
    audit = await _start(project)
    runner = SeoAuditRunner(repos, client_factory=site.client)
    runner.submit(audit["audit_id"])
    await _until(lambda: f"GET {ORIGIN}/b" in site.requests)
    # Another process takes the audit over, as after a missed heartbeat.
    now = int(time.time())
    taken = repos.seo_audit_repo.claim(audit["audit_id"], "other", now, now + 1000, "later")
    assert taken is not None
    await runner.wait(audit["audit_id"])
    gate.set()

    row = await audits.get(user, project_id, audit["audit_id"])
    assert row["status"] == "running"
    with repos.seo_audit_repo._db.connect() as conn:
        token = conn.execute(
            "SELECT runner_token FROM seo_audits WHERE audit_id=?", (audit["audit_id"],)
        ).fetchone()["runner_token"]
    assert token == "other"


async def test_writes_with_a_stale_token_are_rejected(project: Any) -> None:
    _audits, repos, _user, _project_id = project
    repo = repos.seo_audit_repo
    audit = await _start(project)
    now = int(time.time())
    assert repo.claim(audit["audit_id"], "old", now, now - 90, "t") is not None
    # "old" stopped heartbeating; "new" takes over.
    assert repo.claim(audit["audit_id"], "new", now + 200, now + 110, "t") is not None
    seeded = repo.save_discovery(
        audit["audit_id"],
        "old",
        now,
        start_url=f"{ORIGIN}/",
        robots_text=None,
        start=f"{ORIGIN}/",
        seeds=[],
    )
    assert seeded is False
    assert repo.next_url(audit["audit_id"]) is None
    assert repo.beat(audit["audit_id"], "new", now + 201)


async def test_a_blocked_start_url_fails_the_audit(project: Any) -> None:
    audits, repos, user, project_id = project
    site = FakeSite({f"{ORIGIN}/": redirect("http://localhost/admin")})
    audit = await _start(project)
    runner = SeoAuditRunner(repos, client_factory=site.client)
    runner.submit(audit["audit_id"])
    await runner.wait(audit["audit_id"])
    failed = await audits.get(user, project_id, audit["audit_id"])
    assert (failed["status"], failed["error_code"], failed["failed_phase"]) == (
        "failed",
        "target_blocked",
        "discovery",
    )


async def test_start_rules(project: Any, context: Any) -> None:
    audits, repos, user, project_id = project
    _service, _repos, _user, other = context
    first = await _start(project, max_pages=100)
    assert (first["status"], first["requested_url"], first["max_pages"]) == (
        "queued",
        f"{ORIGIN}/",
        100,
    )
    # The same key reads the same audit; different settings under it conflict.
    assert (await _start(project, max_pages=100))["audit_id"] == first["audit_id"]
    with pytest.raises(OctopError) as replayed:
        await _start(project, max_pages=50)
    assert replayed.value.code.value == "SEO_CONFLICT"
    # One audit at a time per project.
    with pytest.raises(OctopError) as busy:
        await _start(project, key="k2")
    assert busy.value.code.value == "SEO_CONFLICT"
    # Cancelling a queued audit nobody runs ends it at once and frees the project.
    cancelled = await audits.cancel(user, project_id, first["audit_id"])
    assert cancelled["status"] == "cancelled"
    assert (await _start(project, key="k2"))["status"] == "queued"

    # Experts start audits with own_site_only: the project domain, with or without www.
    await audits.cancel(user, project_id, (await audits.get(user, project_id))["audit_id"])
    with pytest.raises(OctopError) as other_site:
        await _start(project, key="sub", start_url="https://blog.example.com/", own_site_only=True)
    assert other_site.value.code.value == "SEO_INVALID_INPUT"
    www = await _start(project, key="www", start_url="www.example.com/shop", own_site_only=True)
    assert www["requested_url"] == "https://www.example.com/shop"

    for bad in ("ftp://example.com", "http://127.0.0.1/", "http://intranet.local/"):
        with pytest.raises(OctopError) as invalid:
            await _start(project, key=bad, start_url=bad)
        assert invalid.value.code.value == "SEO_INVALID_INPUT"
    with pytest.raises(OctopError):
        await _start(project, key="k3", max_pages=500)
    with pytest.raises(OctopError) as forbidden:
        await audits.start(other, project_id, idempotency_key="x")
    assert forbidden.value.code.value == "FORBIDDEN"
    with pytest.raises(OctopError) as missing:
        await audits.get(user, project_id, "nope")
    assert missing.value.code.value == "NOT_FOUND"


async def test_agent_tools_read_the_bound_projects_audits(
    project: Any, context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from types import SimpleNamespace
    from unittest.mock import Mock

    from octop.infra.seo.tools import MAX_RESULT_CHARS, build_seo_tools

    _audits, repos, user, project_id = project
    service = context[0]
    configurable = {"user": str(user), "agent_id": "seo", "thread_id": "thread-a"}
    monkeypatch.setattr("octop.infra.seo.tools.get_config", lambda: {"configurable": configurable})
    threads = Mock()
    threads.get.return_value = SimpleNamespace(user_id=user, agent_id="seo")
    tools = {tool.name: tool for tool in build_seo_tools(service, "seo", threads)}

    # The expert may audit only its project's own site, one audit at a time.
    with pytest.raises(OctopError) as elsewhere:
        await tools["seo_start_audit"].ainvoke({"start_url": "https://other.example/"})
    assert elsewhere.value.code.value == "SEO_INVALID_INPUT"
    started = json.loads(await tools["seo_start_audit"].ainvoke({"max_pages": 10}))
    audit = started["audit"]
    assert (audit["status"], audit["max_pages"], audit["requested_url"]) == (
        "queued",
        10,
        f"{ORIGIN}/",
    )
    with pytest.raises(OctopError) as busy:
        await tools["seo_start_audit"].ainvoke({})
    assert busy.value.code.value == "SEO_CONFLICT"

    # Nothing submits it: the runner's sweep picks the queued audit up.
    site = _site()
    runner = SeoAuditRunner(repos, client_factory=site.client)
    runner.start()
    try:
        await _until(
            lambda: repos.seo_audit_repo.get(project_id, audit["audit_id"])["status"] == "completed"
        )
    finally:
        await runner.stop()

    listed = json.loads(await tools["seo_list_audits"].ainvoke({}))
    assert [row["audit_id"] for row in listed["audits"]] == [audit["audit_id"]]

    text = await tools["seo_get_audit_issues"].ainvoke({})
    assert len(text) <= MAX_RESULT_CHARS
    issues = json.loads(text)
    assert issues["audit"]["status"] == "completed"
    broken = next(row for row in issues["summary"] if row["issue_type"] == "broken-internal-link")
    assert broken["how_to_fix"].startswith("Point the link")
    critical = json.loads(await tools["seo_get_audit_issues"].ainvoke({"severity": "critical"}))
    assert {row["severity"] for row in critical["issues"]} == {"critical"}

    pages = json.loads(
        await tools["seo_get_audit_pages"].ainvoke(
            {"audit_id": audit["audit_id"], "url_contains": "/a"}
        )
    )
    assert [row["url"] for row in pages["pages"]] == [f"{ORIGIN}/a"]

    # Another agent's runtime identity is refused before any read.
    configurable["agent_id"] = "intruder"
    with pytest.raises(OctopError):
        await tools["seo_list_audits"].ainvoke({})
