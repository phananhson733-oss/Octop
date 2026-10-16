"""Fetching and discovery, ported from OpenSEO site-audit-workflow-helpers.ts,
classify-fetch.ts, discovery.ts and url-policy.ts (start URL probe).

The client never follows redirects: a crawled redirect is recorded as its own
page and its target queued, and discovery follows at most five hops by hand,
rechecking the crawl policy on each one.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import re
import time
from dataclasses import dataclass, field
from xml.etree import ElementTree

import anyio
import httpx

from octop.infra.seo.audit.analyzer import Link, analyze_html
from octop.infra.seo.audit.robots import MAX_ROBOTS_BYTES, Robots
from octop.infra.seo.audit.rules import PageFacts
from octop.infra.seo.audit.throttle import CrawlThrottle
from octop.infra.seo.audit.urls import (
    TargetBlocked,
    is_crawlable_url,
    is_same_origin,
    normalize_url,
    parse_link_header_canonical,
    validate_start_url,
)

logger = logging.getLogger(__name__)

MAX_HTML_BYTES = 1024 * 1024
MAX_SITEMAP_BYTES = 10 * 1024 * 1024
MAX_SITEMAP_DEPTH = 3
MAX_SITEMAP_DOCS = 300
SITEMAP_CONCURRENCY = 5
SITEMAP_TIMEOUT_S = 15.0
ROBOTS_TIMEOUT_S = 10.0
PROBE_TIMEOUT_S = 10.0
MAX_REDIRECT_HOPS = 5
ACCEPT_HTML = "text/html,application/xhtml+xml"
# Network failures that make a page an "error" row instead of stopping the audit.
FETCH_ERRORS = (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, TargetBlocked, OSError)

# Challenge pages that can arrive with a 200: Cloudflare's interstitial, SiteGround's sgcaptcha.
_CHALLENGE_2XX = re.compile(
    r"<title>\s*(?:just a moment\.\.\.|attention required! \| cloudflare)\s*</title>"
    r"|/\.well-known/sgcaptcha/",
    re.I,
)
_CHALLENGE_MARKERS = (
    "just a moment...",
    "challenge-platform",
    "cf-browser-verification",
    "attention required! | cloudflare",
    "verifying you are human",
)


def classify_fetch(status_code: int, mitigated: bool, body_snippet: str) -> str:
    if status_code == 0:
        return "error"
    # A final 429 is rate limiting, even with cf-mitigated (Cloudflare rate rules set it).
    if status_code == 429:
        return "rate_limited"
    if mitigated or status_code in {401, 403}:
        return "blocked"
    # Real pages are not served as 202 Accepted; bot challenges are.
    if status_code == 202:
        return "blocked"
    if 200 <= status_code < 300 and _CHALLENGE_2XX.search(body_snippet):
        return "blocked"
    if status_code == 503 and any(m in body_snippet.lower() for m in _CHALLENGE_MARKERS):
        return "blocked"
    return "ok"


@dataclass
class CrawledPage:
    url: str
    status_code: int
    fetch_class: str
    response_time_ms: int
    crawl_depth: int | None
    in_sitemap: bool
    redirect_url: str | None = None
    x_robots_tag: str | None = None
    header_canonical_url: str | None = None
    title: str = ""
    meta_description: str = ""
    canonical_url: str | None = None
    robots_meta: str | None = None
    og_title: str | None = None
    og_description: str | None = None
    og_image: str | None = None
    heading_order: list[int] = field(default_factory=list)
    h1_count: int = 0
    word_count: int = 0
    content_hash: str | None = None
    is_html: bool = False
    javascript_shell: bool = False
    images_total: int = 0
    images_missing_alt: int = 0
    links: list[Link] = field(default_factory=list)
    has_structured_data: bool = False
    hreflang_tags: list[str] = field(default_factory=list)
    is_indexable: bool = False
    # A 429 was retried for this URL; not stored.
    rate_limited: bool = False

    def facts(self) -> PageFacts:
        return PageFacts(
            url=self.url,
            status_code=self.status_code,
            fetch_class=self.fetch_class,
            redirect_url=self.redirect_url,
            title=self.title,
            meta_description=self.meta_description,
            canonical_url=self.canonical_url,
            header_canonical_url=self.header_canonical_url,
            robots_meta=self.robots_meta,
            x_robots_tag=self.x_robots_tag,
            h1_count=self.h1_count,
            heading_order=self.heading_order,
            word_count=self.word_count,
            content_hash=self.content_hash,
            is_html=self.is_html,
            javascript_shell=self.javascript_shell,
            images_total=self.images_total,
            images_missing_alt=self.images_missing_alt,
            link_count=len(self.links),
            is_indexable=self.is_indexable,
            response_time_ms=self.response_time_ms,
            crawl_depth=self.crawl_depth,
        )


def _charset(response: httpx.Response) -> str:
    declared = response.charset_encoding
    if declared:
        try:
            return codecs.lookup(declared).name
        except LookupError:
            pass
    return "utf-8"


async def _read_bytes(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    """Up to ``max_bytes`` of the body, and whether more was left unread."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        room = max_bytes - size
        if len(chunk) > room:
            chunks.append(chunk[:room])
            return b"".join(chunks), True
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks), False


async def crawl_page(
    client: httpx.AsyncClient,
    url: str,
    depth: int | None,
    in_sitemap: bool,
    throttle: CrawlThrottle,
) -> CrawledPage | None:
    """Fetch and analyze one URL; None when the site's cooldown stopped the crawl."""
    started = time.monotonic()
    rate_limited = False
    try:
        attempt = 0
        while True:
            attempt += 1
            if not await throttle.ready():
                return None
            sent = time.monotonic()
            async with client.stream("GET", url, headers={"Accept": ACCEPT_HTML}) as response:
                # Measured to the headers of the last attempt, so backoff never looks slow.
                elapsed = int((time.monotonic() - sent) * 1000)
                if response.status_code == 429:
                    rate_limited = attempt > 1
                    if throttle.backoff(attempt, response.headers.get("retry-after")):
                        continue
                    return CrawledPage(
                        url,
                        429,
                        "rate_limited",
                        elapsed,
                        depth,
                        in_sitemap,
                        x_robots_tag=response.headers.get("x-robots-tag"),
                    )
                throttle.recovered()
                rate_limited = attempt > 1
                return await _page_from_response(
                    response, url, elapsed, depth, in_sitemap, rate_limited
                )
    except FETCH_ERRORS:
        return CrawledPage(
            url, 0, "error", int((time.monotonic() - started) * 1000), depth, in_sitemap
        )


async def _page_from_response(
    response: httpx.Response,
    url: str,
    elapsed: int,
    depth: int | None,
    in_sitemap: bool,
    rate_limited: bool,
) -> CrawledPage:
    status = response.status_code
    page = CrawledPage(
        url,
        status,
        "ok",
        elapsed,
        depth,
        in_sitemap,
        x_robots_tag=response.headers.get("x-robots-tag"),
        header_canonical_url=parse_link_header_canonical(response.headers.get("link"), url),
        rate_limited=rate_limited,
    )
    if 300 <= status < 400:
        location = response.headers.get("location")
        page.redirect_url = normalize_url(location, url) if location else None
        return page
    is_html = "text/html" in response.headers.get("content-type", "").lower()
    body = ""
    if is_html:
        # The first 1 MiB still holds the metadata and navigation of normal pages.
        raw, _truncated = await _read_bytes(response, MAX_HTML_BYTES)
        try:
            body = raw.decode(_charset(response), "replace")
        except LookupError:
            # A declared codec that is not a text encoding, such as base64.
            body = raw.decode("utf-8", "replace")
    page.fetch_class = classify_fetch(
        status, bool(response.headers.get("cf-mitigated")), body[:4000]
    )
    if not is_html or page.fetch_class != "ok" or status >= 400:
        return page
    try:
        analysis = await asyncio.get_running_loop().run_in_executor(None, analyze_html, body, url)
    except Exception:
        # One document the parser cannot handle must not fail the whole audit.
        logger.warning("SEO audit could not parse %s", url, exc_info=True)
        return page
    directives = ",".join(v for v in (analysis.robots_meta, page.x_robots_tag) if v).lower()
    page.is_html = True
    page.is_indexable = "noindex" not in directives
    page.title = analysis.title
    page.meta_description = analysis.meta_description
    page.canonical_url = (
        (normalize_url(analysis.canonical, url) or analysis.canonical)
        if analysis.canonical
        else None
    )
    page.robots_meta = analysis.robots_meta
    page.og_title = analysis.og_title
    page.og_description = analysis.og_description
    page.og_image = analysis.og_image
    page.heading_order = analysis.heading_order
    page.h1_count = sum(1 for heading in analysis.h1s if heading)
    page.word_count = analysis.word_count
    page.content_hash = analysis.content_hash
    page.javascript_shell = analysis.javascript_shell
    page.images_total = len(analysis.images)
    # Only a missing alt attribute counts: alt="" is right for decorative images.
    page.images_missing_alt = sum(1 for image in analysis.images if image["alt"] is None)
    page.links = analysis.links
    page.has_structured_data = analysis.has_structured_data
    page.hreflang_tags = analysis.hreflang_tags
    return page


# Discovery ------------------------------------------------------------------------


@dataclass
class Fetched:
    status_code: int
    content_type: str
    body: bytes | None
    final_url: str
    charset: str


async def _fetch_following(
    client: httpx.AsyncClient, url: str, timeout: float, max_bytes: int, *, truncate: bool
) -> Fetched | None:
    """GET with redirects followed by hand, each hop rechecked; one deadline for the chain.

    The body is None when it exceeds ``max_bytes`` and ``truncate`` is False.
    """
    current = url
    with anyio.fail_after(timeout):
        for _hop in range(MAX_REDIRECT_HOPS + 1):
            async with client.stream("GET", current) as response:
                if not 300 <= response.status_code < 400:
                    body: bytes | None
                    body, more = await _read_bytes(response, max_bytes)
                    if more and not truncate:
                        body = None
                    return Fetched(
                        response.status_code,
                        response.headers.get("content-type", ""),
                        body,
                        current,
                        _charset(response),
                    )
                location = response.headers.get("location")
            if not location:
                return None
            following = normalize_url(location, current)
            if following is None or not is_crawlable_url(following):
                return None
            current = following
    return None


async def fetch_robots_text(client: httpx.AsyncClient, origin: str) -> str | None:
    """robots.txt text, or None when missing or unreachable (everything allowed)."""
    try:
        fetched = await _fetch_following(
            client, origin + "/robots.txt", ROBOTS_TIMEOUT_S, MAX_ROBOTS_BYTES, truncate=True
        )
    except (*FETCH_ERRORS, TimeoutError):
        return None
    if fetched is None or not 200 <= fetched.status_code < 300 or fetched.body is None:
        return None
    return fetched.body.decode(fetched.charset, "replace")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_sitemap(body: bytes, base: str) -> tuple[list[str], list[str]]:
    """``(nested sitemaps, page URLs)``; documents with a DTD are refused unparsed."""
    if re.search(rb"<!(?:DOCTYPE|ENTITY)", body, re.I):
        return [], []
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return [], []
    kind = {"sitemapindex": "sitemap", "urlset": "url"}.get(_local(root.tag))
    found: list[str] = []
    for entry in root:
        if _local(entry.tag) != kind:
            continue
        loc = next((child.text for child in entry if _local(child.tag) == "loc"), None)
        normalized = normalize_url(loc.strip(), base) if loc else None
        if normalized:
            found.append(normalized)
    return (found, []) if kind == "sitemap" else ([], found)


def _looks_like_sitemap(content_type: str, text: str) -> bool:
    if "xml" in content_type.lower():
        return True
    head = text.lstrip()[:20].lower()
    return head.startswith(("<?xml", "<urlset", "<sitemapindex"))


async def _sitemap_document(client: httpx.AsyncClient, url: str) -> tuple[list[str], list[str]]:
    for attempt in range(2):
        try:
            fetched = await _fetch_following(
                client, url, SITEMAP_TIMEOUT_S, MAX_SITEMAP_BYTES, truncate=False
            )
        except TimeoutError:
            if attempt == 0:
                continue
            return [], []
        except FETCH_ERRORS:
            return [], []
        if (
            fetched is None
            or not is_same_origin(fetched.final_url, url)
            or not 200 <= fetched.status_code < 300
            or fetched.body is None
            or not _looks_like_sitemap(
                fetched.content_type, fetched.body[:64].decode("utf-8", "replace")
            )
        ):
            return [], []
        return await asyncio.get_running_loop().run_in_executor(
            None, parse_sitemap, fetched.body, fetched.final_url
        )
    return [], []


async def discover_sitemap_urls(
    client: httpx.AsyncClient, origin: str, robots: Robots, max_pages: int
) -> list[str]:
    """Same-origin page URLs from robots.txt sitemaps and /sitemap.xml, sitemap indexes
    followed three levels deep. Every URL is returned, not only the first ``max_pages``:
    seeding is what marks a page as listed in the sitemap.
    """
    limit = min(max(max_pages * 20, 500), 50_000)
    sources = (
        normalize_url(source, origin) for source in [*robots.sitemaps, origin + "/sitemap.xml"]
    )
    queue = [
        (url, MAX_SITEMAP_DEPTH)
        for url in dict.fromkeys(sources)
        if url and is_same_origin(url, origin)
    ]
    seen_docs: set[str] = set()
    urls: dict[str, None] = {}
    fetched_docs = 0
    while queue and len(urls) < limit and fetched_docs < MAX_SITEMAP_DOCS:
        batch, queue = queue[:SITEMAP_CONCURRENCY], queue[SITEMAP_CONCURRENCY:]
        batch = [(url, depth) for url, depth in batch if depth > 0 and url not in seen_docs]
        seen_docs.update(url for url, _depth in batch)
        fetched_docs += len(batch)
        results = await asyncio.gather(*(_sitemap_document(client, url) for url, _ in batch))
        for (_url, depth), (nested, pages) in zip(batch, results, strict=True):
            for page_url in pages:
                if len(urls) >= limit:
                    break
                if is_same_origin(page_url, origin):
                    urls[page_url] = None
            if depth > 1:
                queue.extend(
                    (sitemap, depth - 1)
                    for sitemap in nested
                    if is_same_origin(sitemap, origin) and sitemap not in seen_docs
                )
    return list(urls)


async def resolve_start_url(client: httpx.AsyncClient, start_url: str) -> str:
    """Follow the start URL's redirects so the audit anchors to the site's real origin.

    Each hop is revalidated; a hop into a private network raises ``TargetBlocked``.
    A failed probe keeps the last good URL, and the crawl records the real result.
    """
    current = start_url
    for _hop in range(MAX_REDIRECT_HOPS):
        try:
            with anyio.fail_after(PROBE_TIMEOUT_S):
                response = await client.head(current)
        except (httpx.HTTPError, httpx.InvalidURL, OSError, TimeoutError):
            return current
        location = response.headers.get("location")
        if not 300 <= response.status_code < 400 or not location:
            return current
        following = normalize_url(location, current)
        if following is None:
            return current
        current = validate_start_url(following)
    return current


def seeds_for(
    start_url: str, origin: str, robots: Robots, sitemap_urls: list[str], user_agent: str
) -> tuple[str | None, list[str]]:
    """The start URL (if crawlable) and the sitemap seeds that pass the crawl policy."""
    start = (
        start_url
        if robots.is_allowed(start_url, user_agent) and is_same_origin(start_url, origin)
        else None
    )
    seeds = [
        url
        for url in dict.fromkeys(sitemap_urls)
        if is_same_origin(url, origin)
        and is_crawlable_url(url)
        and robots.is_allowed(url, user_agent)
    ]
    return start, seeds


def queueable(url: str, origin: str, robots: Robots, user_agent: str) -> bool:
    return (
        is_same_origin(url, origin) and is_crawlable_url(url) and robots.is_allowed(url, user_agent)
    )
