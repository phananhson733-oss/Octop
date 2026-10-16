"""In-process audit runner: durable, fenced, resumable.

An audit row is claimed with this process's token before any work. A heartbeat
keeps the claim fresh; every crawl write checks the token, so if another process
takes over (a deploy overlap, a second worker) or the user cancels or deletes the
audit, this one stops at its next write. On shutdown the claim is released and the
row stays ``running``; the next process picks it up from its stored frontier.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any

import httpx

from octop.infra.db.services import RepoBundle
from octop.infra.seo.audit.analyzer import MAX_EXTRACTED_LINKS
from octop.infra.seo.audit.crawler import (
    MAX_BLOCKED_SITEMAP_ISSUES,
    CrawledPage,
    blocked_for_google,
    crawl_page,
    discover_sitemap_urls,
    fetch_robots_text,
    queueable,
    resolve_start_url,
    seeds_for,
)
from octop.infra.seo.audit.net import USER_AGENT, crawler_client
from octop.infra.seo.audit.render import Rendered, RendererFactory, RenderUnavailable
from octop.infra.seo.audit.robots import Robots, parse_robots
from octop.infra.seo.audit.rules import (
    FINALIZE_TYPES,
    Issue,
    PageFacts,
    find_broken_links,
    find_canonical_problems,
    find_duplicates,
    find_hreflang_problems,
    find_missing_from_sitemap,
    find_orphans,
    find_redirect_chains_and_loops,
    find_render_problems,
    find_sitemap_problems,
    page_issues,
)
from octop.infra.seo.audit.throttle import CrawlThrottle
from octop.infra.seo.audit.urls import (
    TargetBlocked,
    is_same_origin,
    normalize_url,
    origin_of,
    path_cluster,
)
from octop.infra.utils.ulid import new_ulid

logger = logging.getLogger(__name__)

# A claim whose heartbeat is older than this is free to take over.
STALE_S = 90
BEAT_S = 20.0
SWEEP_S = 30.0
MAX_CONCURRENT_AUDITS = 2
# Mega-menu pages can carry 1000+ links; the link checks only need these.
MAX_STORED_LINKS_PER_PAGE = 500
# Rendering takes seconds a page: a sample, the start page first, then the page
# templates in turns, scaled to the crawl budget.
MIN_RENDERED_PAGES = 5
MAX_RENDERED_PAGES = 20
# Internal links only the rendered DOM has, which the crawl never fetched, get a status check.
MAX_RENDERED_LINK_CHECKS = 30
MAX_STORED_RENDERED_ONLY = 100


class Fenced(Exception):
    """The audit was taken over, cancelled or deleted; stop without writing."""


async def _db[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    return await asyncio.get_running_loop().run_in_executor(None, partial(fn, *args, **kwargs))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _issue_row(issue: Issue) -> dict[str, Any]:
    return {
        "issue_type": issue.issue_type,
        "severity": issue.severity,
        "page_url": issue.page_url,
        "details": issue.details,
    }


def page_row(page: CrawledPage, internal_links: list[str]) -> dict[str, Any]:
    internal = sum(1 for link in page.links if link.is_internal)
    return {
        "url": page.url,
        "status_code": page.status_code,
        "fetch_class": page.fetch_class,
        "redirect_url": page.redirect_url,
        "title": page.title,
        "meta_description": page.meta_description,
        "canonical_url": page.canonical_url,
        "header_canonical_url": page.header_canonical_url,
        "robots_meta": page.robots_meta,
        "x_robots_tag": page.x_robots_tag,
        "og_title": page.og_title,
        "og_description": page.og_description,
        "og_image": page.og_image,
        "h1_count": page.h1_count,
        "heading_order": page.heading_order,
        "word_count": page.word_count,
        "content_hash": page.content_hash,
        "is_html": page.is_html,
        "javascript_shell": page.javascript_shell,
        "images_total": page.images_total,
        "images_missing_alt": page.images_missing_alt,
        "internal_link_count": internal,
        "external_link_count": len(page.links) - internal,
        "has_structured_data": page.has_structured_data,
        "hreflang": page.hreflang_tags,
        "is_indexable": page.is_indexable,
        "response_time_ms": page.response_time_ms,
        "crawl_depth": page.crawl_depth,
        "in_sitemap": page.in_sitemap,
        "crawled_at": _now(),
        "h1s": page.h1s,
        "alternates": page.alternates,
        "body_truncated": page.body_truncated,
        "internal_links": internal_links,
    }


def facts_from_row(row: dict[str, Any]) -> PageFacts:
    return PageFacts(
        url=row["url"],
        status_code=row["status_code"],
        fetch_class=row["fetch_class"],
        redirect_url=row["redirect_url"],
        title=row["title"],
        meta_description=row["meta_description"],
        canonical_url=row["canonical_url"],
        header_canonical_url=row["header_canonical_url"],
        robots_meta=row["robots_meta"],
        x_robots_tag=row["x_robots_tag"],
        h1_count=row["h1_count"],
        heading_order=row["heading_order"],
        word_count=row["word_count"],
        content_hash=row["content_hash"],
        is_html=row["is_html"],
        javascript_shell=row["javascript_shell"],
        images_total=row["images_total"],
        images_missing_alt=row["images_missing_alt"],
        link_count=row["internal_link_count"] + row["external_link_count"],
        is_indexable=row["is_indexable"],
        response_time_ms=row["response_time_ms"],
        crawl_depth=row["crawl_depth"],
        in_sitemap=row["in_sitemap"],
        h1s=row["h1s"],
        alternates=[(lang, href) for lang, href in row["alternates"]],
        body_truncated=row["body_truncated"],
    )


def render_sample(rows: list[dict[str, Any]], start_url: str, max_pages: int) -> list[str]:
    """The pages to render: the start page, then one page per template in turns."""
    limit = max(MIN_RENDERED_PAGES, min(MAX_RENDERED_PAGES, max_pages // 5))
    clusters: dict[str, list[str]] = defaultdict(list)
    for row in sorted(rows, key=lambda row: row["url"] != start_url):
        if row["fetch_class"] == "ok" and row["status_code"] == 200 and row["is_html"]:
            clusters[path_cluster(row["url"])].append(row["url"])
    sample: list[str] = []
    for turn in range(limit):
        for urls in clusters.values():
            if turn < len(urls) and len(sample) < limit:
                sample.append(urls[turn])
    return sample


def _links_complete(row: dict[str, Any]) -> bool:
    """Whether the stored links are all the page's HTML has, so a diff against them is fair."""
    return (
        not row["body_truncated"]
        and row["internal_link_count"] <= len(row["internal_links"])
        and row["internal_link_count"] + row["external_link_count"] < MAX_EXTRACTED_LINKS
    )


class AuditJob:
    def __init__(
        self,
        repos: RepoBundle,
        audit: dict[str, Any],
        token: str,
        client: httpx.AsyncClient,
        renderer_factory: RendererFactory | None = None,
        render_lock: asyncio.Lock | None = None,
    ) -> None:
        self.repo = repos.seo_audit_repo
        self.audit = audit
        self.id = str(audit["audit_id"])
        self.token = token
        self.client = client
        self.phase = str(audit["phase"])
        self.renderer_factory = renderer_factory
        self.render_lock = render_lock or asyncio.Lock()

    async def _fenced(self, ok: bool) -> None:
        if not ok:
            raise Fenced(self.id)

    async def run(self) -> None:
        if self.phase == "discovery":
            await self._discover()
        start_url = str(self.audit["start_url"])
        origin = origin_of(start_url)
        robots = parse_robots(origin, self.audit["robots_text"])
        if self.phase == "crawl":
            complete, rate_limited = await self._crawl(origin, robots)
            self.phase = "render"
        else:
            complete = bool(self.audit["crawl_complete"])
            rate_limited = bool(self.audit["rate_limited"])
        if self.phase == "render":
            # A site that is already throttling the crawl gets no more requests.
            if self.renderer_factory is not None and not rate_limited:
                await self._render(start_url, origin, robots)
            await self._fenced(
                await _db(self.repo.start_finalize, self.id, self.token, int(time.time()))
            )
        self.phase = "finalize"
        await self._finalize(start_url, complete, rate_limited)

    async def _discover(self) -> None:
        start_url = await resolve_start_url(self.client, str(self.audit["requested_url"]))
        origin = origin_of(start_url)
        robots_text = await fetch_robots_text(self.client, origin)
        robots = parse_robots(origin, robots_text)
        sitemap_urls = await discover_sitemap_urls(
            self.client, origin, robots, int(self.audit["max_pages"])
        )
        start, seeds = seeds_for(start_url, origin, robots, sitemap_urls, USER_AGENT)
        blocked = blocked_for_google(sitemap_urls, origin, robots)[:MAX_BLOCKED_SITEMAP_ISSUES]
        await self._fenced(
            await _db(
                self.repo.save_discovery,
                self.id,
                self.token,
                int(time.time()),
                start_url=start_url,
                robots_text=robots_text,
                start=(start, path_cluster(start)) if start is not None else None,
                seeds=[(url, path_cluster(url)) for url in seeds],
                issues=[_issue_row(Issue("sitemap-blocked-by-robots", url)) for url in blocked],
            )
        )
        self.audit.update(start_url=start_url, robots_text=robots_text, sitemap_count=len(seeds))
        self.phase = "crawl"

    async def _crawl(self, origin: str, robots: Robots) -> tuple[bool, bool]:
        throttle = CrawlThrottle()
        crawled = int(self.audit["pages_crawled"])
        while crawled < int(self.audit["max_pages"]):
            entry = await _db(self.repo.next_url, self.id)
            if entry is None:
                break
            depth = entry["depth"]
            page = await crawl_page(self.client, entry["url"], depth, entry["in_sitemap"], throttle)
            if page is None:
                # The site's cooldown stopped the crawl; this URL stays queued.
                break
            issues = [_issue_row(issue) for issue in page_issues(page.facts())]
            targets: list[str] = []
            discovered: dict[str, int | None] = {}
            for link in page.links:
                if not link.is_internal:
                    continue
                if len(targets) < MAX_STORED_LINKS_PER_PAGE:
                    targets.append(link.target_url)
                if link.target_url not in discovered and queueable(
                    link.target_url, origin, robots, USER_AGENT
                ):
                    discovered[link.target_url] = depth + 1 if depth is not None else None
            # Canonical and hreflang targets are queued too, for the cross-page checks; no
            # click leads there, so they carry no depth and wait behind linked pages.
            for related in [page.canonical_url, *(href for _lang, href in page.alternates)]:
                if (
                    related
                    and related != page.url
                    and related not in discovered
                    and queueable(related, origin, robots, USER_AGENT)
                ):
                    discovered[related] = None
            # A redirect continues the same navigation path: same depth.
            if (
                page.redirect_url
                and page.redirect_url not in discovered
                and queueable(page.redirect_url, origin, robots, USER_AGENT)
            ):
                discovered[page.redirect_url] = depth
            await self._fenced(
                await _db(
                    self.repo.record_page,
                    self.id,
                    self.token,
                    int(time.time()),
                    page_row(page, targets),
                    issues,
                    [(url, found, path_cluster(url)) for url, found in discovered.items()],
                )
            )
            crawled += 1
        pending = await _db(self.repo.pending_count, self.id)
        rate_limited = throttle.stopped
        complete = pending == 0 and not rate_limited
        await self._fenced(
            await _db(
                self.repo.end_crawl,
                self.id,
                self.token,
                int(time.time()),
                crawl_complete=complete,
                rate_limited=rate_limited,
            )
        )
        return complete, rate_limited

    async def _render(self, start_url: str, origin: str, robots: Robots) -> None:
        assert self.renderer_factory is not None
        rows = await _db(self.repo.pages, self.id, with_links=True)
        done = await _db(self.repo.renders, self.id)
        sample = [
            url
            for url in render_sample(rows, start_url, int(self.audit["max_pages"]))
            if url not in done
        ]
        if not sample:
            return
        by_url = {row["url"]: row for row in rows}
        checks = _LinkChecks(self.client, origin, robots, by_url)
        # One browser at a time in this process, whatever the number of audits.
        async with self.render_lock:
            try:
                async with self.renderer_factory(start_url) as renderer:
                    for url in sample:
                        rendered = await renderer.render(url)
                        stored = await _stored_render(rendered, by_url[url], checks)
                        await self._record_render(url, stored)
            except RenderUnavailable as exc:
                await self._record_render(sample[0], {"error": str(exc), "unavailable": True})

    async def _record_render(self, url: str, render: dict[str, Any]) -> None:
        await self._fenced(
            await _db(self.repo.record_render, self.id, self.token, int(time.time()), url, render)
        )

    async def _finalize(self, start_url: str, complete: bool, rate_limited: bool) -> None:
        rows = await _db(self.repo.pages, self.id, with_links=True)
        pages = {row["url"]: facts_from_row(row) for row in rows}
        # An unrendered app shell's placeholder title must not become a duplicate finding.
        readable = [page for page in pages.values() if not page.javascript_shell]
        links = [(row["url"], target) for row in rows for target in row["internal_links"]]
        issues = [
            *find_duplicates(readable),
            *find_redirect_chains_and_loops(readable),
            *find_broken_links(pages, links),
        ]
        # Orphans need the whole frontier and every page's real links.
        if complete and len(readable) == len(pages):
            issues += find_orphans(pages, links, start_url)
        if self.audit["sitemap_count"]:
            issues += find_missing_from_sitemap(pages.values())
        issues += find_sitemap_problems(pages.values())
        issues += find_canonical_problems(pages)
        issues += find_hreflang_problems(pages)
        issues += find_render_problems(pages, await _db(self.repo.renders, self.id))
        if rate_limited:
            issues.append(Issue("crawl-rate-limited", start_url))
        await self._fenced(
            await _db(
                self.repo.complete,
                self.id,
                self.token,
                int(time.time()),
                _now(),
                [_issue_row(issue) for issue in issues],
                FINALIZE_TYPES,
            )
        )


class _LinkChecks:
    """Status checks for links only the rendered DOM has, through the crawl's own client."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        origin: str,
        robots: Robots,
        crawled: dict[str, dict[str, Any]],
    ) -> None:
        self.client = client
        self.origin = origin
        self.robots = robots
        self.crawled = crawled
        self.left = MAX_RENDERED_LINK_CHECKS
        self.throttle = CrawlThrottle()

    async def statuses(self, links: list[str]) -> dict[str, int]:
        found: dict[str, int] = {}
        for link in links:
            if self.left <= 0 or self.throttle.stopped:
                break
            if link in self.crawled or not queueable(link, self.origin, self.robots, USER_AGENT):
                continue
            self.left -= 1
            page = await crawl_page(self.client, link, None, False, self.throttle)
            # Only a fetch that reached the site is evidence; a block or a cooldown is not.
            if page is not None and page.fetch_class == "ok":
                found[link] = page.status_code
        return found


async def _stored_render(
    rendered: Rendered, row: dict[str, Any], checks: _LinkChecks
) -> dict[str, Any]:
    """What finalize needs from one render: the page state, and links the HTML lacks."""
    stored = rendered.to_json()
    links = stored.pop("links")
    canonical = rendered.canonical
    stored["canonical"] = (normalize_url(canonical) or canonical) if canonical else None
    if rendered.error is not None:
        return stored
    internal = list(
        dict.fromkeys(
            target
            for target in (normalize_url(link) for link in links)
            if target and is_same_origin(target, row["url"])
        )
    )
    stored["internal_links"] = len(internal)
    stored["rendered_only"] = None
    if _links_complete(row):
        html_links = set(row["internal_links"])
        only = [link for link in internal if link not in html_links and link != row["url"]]
        stored["rendered_only_count"] = len(only)
        stored["rendered_only"] = only[:MAX_STORED_RENDERED_ONLY]
        stored["link_status"] = await checks.statuses(stored["rendered_only"])
    return stored


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, TargetBlocked):
        return "target_blocked"
    module = type(exc).__module__
    if module.startswith(("sqlite3", "psycopg")):
        return "db_error"
    return "unknown"


class SeoAuditRunner:
    def __init__(
        self,
        repos: RepoBundle,
        client_factory: Callable[[], httpx.AsyncClient] = crawler_client,
        max_concurrent: int = MAX_CONCURRENT_AUDITS,
        renderer_factory: RendererFactory | None = None,
    ) -> None:
        """``renderer_factory`` opens a browser per audit; without one, nothing is rendered."""
        self.repos = repos
        self.token = new_ulid()
        self._client_factory = client_factory
        self._renderer_factory = renderer_factory
        self._render_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(max_concurrent)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._sweeper: asyncio.Task[None] | None = None
        self._closing = False

    def start(self) -> None:
        """Begin sweeping for queued audits and claims left behind by stopped processes."""
        self._sweeper = asyncio.create_task(self._sweep(), name="seo-audit-sweep")

    async def _sweep(self) -> None:
        while not self._closing:
            try:
                for audit_id in await _db(
                    self.repos.seo_audit_repo.claimable, int(time.time()) - STALE_S
                ):
                    self.submit(audit_id)
            except Exception:
                logger.exception("SEO audit sweep failed")
            await asyncio.sleep(SWEEP_S)

    def submit(self, audit_id: str) -> None:
        if self._closing or audit_id in self._tasks:
            return
        task = asyncio.create_task(self._run(audit_id), name=f"seo-audit-{audit_id}")
        self._tasks[audit_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(audit_id, None))

    async def wait(self, audit_id: str) -> None:
        """Wait for an audit this process is running (tests and shutdown)."""
        task = self._tasks.get(audit_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def stop(self) -> None:
        self._closing = True
        pending = [task for task in [self._sweeper, *self._tasks.values()] if task is not None]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _run(self, audit_id: str) -> None:
        repo = self.repos.seo_audit_repo
        async with self._slots:
            now = int(time.time())
            claim: asyncio.Task[dict[str, Any] | None] = asyncio.ensure_future(
                _db(repo.claim, audit_id, self.token, now, now - STALE_S, _now())
            )
            try:
                audit = await asyncio.shield(claim)
            except asyncio.CancelledError:
                # Shutdown during the claim: hand back a claim that still landed.
                await asyncio.wait([claim])
                if claim.exception() is None and claim.result() is not None:
                    await asyncio.shield(_db(repo.release, audit_id, self.token))
                raise
            if audit is None:
                return
            lost = False

            async def beat(job: asyncio.Task[None]) -> None:
                nonlocal lost
                while True:
                    await asyncio.sleep(BEAT_S)
                    try:
                        held = await _db(repo.beat, audit_id, self.token, int(time.time()))
                    except Exception:
                        # A database hiccup is not a lost claim; the next beat retries.
                        logger.warning("SEO audit %s heartbeat failed", audit_id, exc_info=True)
                        continue
                    if not held:
                        lost = True
                        job.cancel()
                        return

            async with self._client_factory() as client:
                runner = AuditJob(
                    self.repos,
                    audit,
                    self.token,
                    client,
                    self._renderer_factory,
                    self._render_lock,
                )
                job = asyncio.create_task(runner.run())
                beater = asyncio.create_task(beat(job))
                try:
                    await job
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        # Shutdown: let the page in flight stop, then hand the claim back so
                        # the next process resumes at once.
                        job.cancel()
                        await asyncio.shield(asyncio.gather(job, return_exceptions=True))
                        await asyncio.shield(_db(repo.release, audit_id, self.token))
                        raise
                    if not lost:
                        raise
                    await _db(repo.mark_cancelled, audit_id, self.token, _now())
                except Fenced:
                    await _db(repo.mark_cancelled, audit_id, self.token, _now())
                except Exception as exc:
                    logger.warning("SEO audit %s failed: %s", audit_id, exc, exc_info=True)
                    await _db(
                        repo.fail,
                        audit_id,
                        self.token,
                        _error_code(exc),
                        str(exc) or type(exc).__name__,
                        runner.phase,
                        _now(),
                    )
                finally:
                    beater.cancel()
