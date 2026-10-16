"""Audit issue rules, ported from OpenSEO page-reporters.ts and multipage-checks.ts.

Page rules run on one crawled page as it is stored. Cross-page rules run once at
finalize over the stored pages and internal links; their issue types never come
from page rules, so finalize can replace exactly its own rows on a retry.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

Severity = Literal["critical", "warning", "info"]

# Registry order is upstream's (audit-issues.ts); copy lives in i18n seo.audit.issues.
ISSUE_SEVERITY: dict[str, Severity] = {
    "blocked-page": "critical",
    "javascript-rendering-suspected": "warning",
    "rate-limited-page": "warning",
    "crawl-rate-limited": "warning",
    "server-error": "critical",
    "broken-internal-link": "critical",
    "missing-title": "critical",
    "broken-page": "warning",
    "duplicate-title": "warning",
    "duplicate-meta-description": "warning",
    "duplicate-content": "warning",
    "missing-meta-description": "warning",
    "missing-h1": "warning",
    "multiple-h1": "warning",
    "redirect-chain": "warning",
    "redirect-loop": "warning",
    "canonical-conflict": "warning",
    "thin-content": "warning",
    "images-missing-alt": "warning",
    "orphan-page": "warning",
    "no-outgoing-links": "warning",
    "title-too-long": "info",
    "title-too-short": "info",
    "meta-description-too-long": "info",
    "meta-description-too-short": "info",
    "heading-order-skip": "info",
    "slow-response": "info",
    "noindex-page": "info",
    "canonicalized-page": "info",
    "deep-page": "info",
    # Octop additions, not in upstream's registry.
    "not-in-sitemap": "warning",
}
SEVERITY_ORDER: dict[str, int] = {"critical": 0, "warning": 1, "info": 2}
FINALIZE_TYPES = frozenset(
    {
        "duplicate-title",
        "duplicate-meta-description",
        "duplicate-content",
        "redirect-chain",
        "redirect-loop",
        "broken-internal-link",
        "orphan-page",
        "crawl-rate-limited",
        "not-in-sitemap",
    }
)

TITLE_MAX_CHARS = 60
TITLE_MIN_CHARS = 10
META_DESCRIPTION_MAX_CHARS = 160
META_DESCRIPTION_MIN_CHARS = 70
THIN_CONTENT_WORDS = 150
SLOW_RESPONSE_MS = 1500
DEEP_PAGE_DEPTH = 5
DUPLICATE_GROUP_SAMPLE = 3
BROKEN_LINK_ISSUE_CAP = 2000


@dataclass
class Issue:
    issue_type: str
    page_url: str
    details: dict[str, Any] | None = None

    @property
    def severity(self) -> Severity:
        return ISSUE_SEVERITY[self.issue_type]


@dataclass
class PageFacts:
    """What the rules read from one crawled page."""

    url: str
    status_code: int
    fetch_class: str
    redirect_url: str | None = None
    title: str = ""
    meta_description: str = ""
    canonical_url: str | None = None
    header_canonical_url: str | None = None
    robots_meta: str | None = None
    x_robots_tag: str | None = None
    h1_count: int = 0
    heading_order: list[int] = field(default_factory=list)
    word_count: int = 0
    content_hash: str | None = None
    is_html: bool = False
    javascript_shell: bool = False
    images_total: int = 0
    images_missing_alt: int = 0
    link_count: int = 0
    is_indexable: bool = False
    response_time_ms: int = 0
    crawl_depth: int | None = None
    in_sitemap: bool = False


def _heading_skip(order: list[int]) -> bool:
    return any(later > earlier + 1 for earlier, later in zip(order, order[1:], strict=False))


def page_issues(page: PageFacts) -> list[Issue]:
    issues: list[Issue] = []

    def report(issue_type: str, details: dict[str, Any] | None = None) -> None:
        issues.append(Issue(issue_type, page.url, details))

    if page.fetch_class == "blocked":
        report("blocked-page", {"status_code": page.status_code})
        return issues
    if page.fetch_class == "rate_limited":
        report("rate-limited-page", {"status_code": page.status_code})
        return issues
    if page.fetch_class == "error":
        return issues
    if page.status_code >= 500:
        report("server-error", {"status_code": page.status_code})
        return issues
    if page.status_code >= 400:
        report("broken-page", {"status_code": page.status_code})
        return issues
    # Redirects are normal alone; chains and loops are cross-page findings.
    if page.status_code >= 300:
        return issues
    if page.response_time_ms > SLOW_RESPONSE_MS:
        report("slow-response", {"response_time_ms": page.response_time_ms})
    # Content checks only make sense for analyzed HTML (a PDF has no title to miss).
    if not page.is_html:
        return issues

    if not page.is_indexable:
        report("noindex-page", {"robots_meta": page.robots_meta, "x_robots_tag": page.x_robots_tag})
    if (
        page.canonical_url
        and page.header_canonical_url
        and page.canonical_url != page.header_canonical_url
    ):
        report(
            "canonical-conflict",
            {
                "html_canonical": page.canonical_url,
                "header_canonical": page.header_canonical_url,
            },
        )
    effective = page.canonical_url or page.header_canonical_url
    if effective and effective != page.url:
        report("canonicalized-page", {"canonical_url": effective})
    if page.crawl_depth is not None and page.crawl_depth >= DEEP_PAGE_DEPTH:
        report("deep-page", {"crawl_depth": page.crawl_depth})
    # An unrendered app shell cannot show what the page lacks: report the gap instead.
    if page.javascript_shell:
        report("javascript-rendering-suspected", {"word_count": page.word_count})
        return issues

    if not page.title:
        report("missing-title")
    elif len(page.title) > TITLE_MAX_CHARS:
        report("title-too-long", {"length": len(page.title)})
    elif len(page.title) < TITLE_MIN_CHARS:
        report("title-too-short", {"length": len(page.title)})

    if not page.meta_description:
        report("missing-meta-description")
    elif len(page.meta_description) > META_DESCRIPTION_MAX_CHARS:
        report("meta-description-too-long", {"length": len(page.meta_description)})
    elif len(page.meta_description) < META_DESCRIPTION_MIN_CHARS:
        report("meta-description-too-short", {"length": len(page.meta_description)})

    if page.h1_count == 0:
        report("missing-h1")
    elif page.h1_count > 1:
        report("multiple-h1", {"h1_count": page.h1_count})
    if _heading_skip(page.heading_order):
        report("heading-order-skip")

    if page.is_indexable and page.word_count < THIN_CONTENT_WORDS:
        report("thin-content", {"word_count": page.word_count})
    if page.images_missing_alt > 0:
        report(
            "images-missing-alt",
            {"images_missing_alt": page.images_missing_alt, "images_total": page.images_total},
        )
    if page.is_indexable and page.link_count == 0:
        report("no-outgoing-links")
    return issues


def _ok_html(page: PageFacts) -> bool:
    return page.fetch_class == "ok" and 200 <= page.status_code < 300


def find_duplicates(pages: list[PageFacts]) -> list[Issue]:
    """Pages the owner already de-duplicated (noindex, canonicalized away) are left out."""
    candidates = [
        page
        for page in pages
        if _ok_html(page)
        and page.is_indexable
        and (page.canonical_url or page.header_canonical_url or page.url) == page.url
    ]
    issues: list[Issue] = []

    def emit(issue_type: str, key_of: Any) -> None:
        groups: dict[str, list[PageFacts]] = {}
        for page in candidates:
            key = key_of(page)
            if key:
                groups.setdefault(key, []).append(page)
        for group in groups.values():
            if len(group) < 2:
                continue
            for page in group:
                others = [other.url for other in group if other is not page]
                issues.append(
                    Issue(
                        issue_type,
                        page.url,
                        {
                            "group_size": len(group),
                            "other_urls": others[:DUPLICATE_GROUP_SAMPLE],
                        },
                    )
                )

    emit("duplicate-title", lambda page: page.title)
    emit("duplicate-meta-description", lambda page: page.meta_description)
    emit("duplicate-content", lambda page: page.content_hash if page.word_count > 0 else None)
    return issues


def find_redirect_chains_and_loops(pages: list[PageFacts]) -> list[Issue]:
    redirects = {
        page.url: page for page in pages if 300 <= page.status_code < 400 and page.redirect_url
    }
    targets = {page.redirect_url for page in redirects.values()}
    issues: list[Issue] = []
    walked: set[str] = set()
    # Walk from heads (redirects nothing redirects to): a 5-hop chain is one issue.
    for url, head in redirects.items():
        if url in targets:
            continue
        hops, seen = [url], {url}
        walked.add(url)
        current = head.redirect_url
        loop = False
        while current:
            hops.append(current)
            if current in seen:
                loop = True
                break
            seen.add(current)
            if current in redirects:
                walked.add(current)
            following = redirects.get(current)
            current = following.redirect_url if following else None
        if loop:
            issues.append(Issue("redirect-loop", url, {"hops": hops}))
        elif len(hops) > 2:
            issues.append(Issue("redirect-chain", url, {"hops": hops, "final_url": hops[-1]}))
    # Cycles with no head (a<->b, a->a) are never reached above: one loop issue each.
    for url in redirects:
        if url in walked:
            continue
        cycle: list[str] = []
        current_url: str | None = url
        while current_url and current_url not in walked:
            walked.add(current_url)
            cycle.append(current_url)
            following = redirects.get(current_url)
            current_url = following.redirect_url if following else None
        issues.append(Issue("redirect-loop", url, {"hops": [*cycle, url]}))
    return issues


def find_broken_links(pages: dict[str, PageFacts], links: Iterable[tuple[str, str]]) -> list[Issue]:
    """Internal links to pages we crawled and saw fail; a blocked target is no evidence."""
    issues: list[Issue] = []
    for source, target in sorted(links):
        page = pages.get(target)
        if page is None or page.fetch_class != "ok" or page.status_code < 400:
            continue
        issues.append(
            Issue(
                "broken-internal-link",
                source,
                {"target_url": target, "target_status": page.status_code},
            )
        )
        if len(issues) >= BROKEN_LINK_ISSUE_CAP:
            break
    return issues


def find_orphans(
    pages: dict[str, PageFacts], links: Iterable[tuple[str, str]], start_url: str
) -> list[Issue]:
    """Live pages no other crawled page links to and nothing redirects to.

    Only meaningful when the crawl finished its frontier, so callers gate it.
    """
    inbound = {target for source, target in links if source != target}
    redirected = {page.redirect_url for page in pages.values() if page.redirect_url}
    return [
        Issue("orphan-page", url)
        for url, page in pages.items()
        if url != start_url and _ok_html(page) and url not in inbound and url not in redirected
    ]


def find_missing_from_sitemap(pages: Iterable[PageFacts]) -> list[Issue]:
    """Indexable pages that are their own canonical yet no sitemap lists.

    Callers run it only when the site has a sitemap: without one, nothing is missing from it.
    """
    return [
        Issue("not-in-sitemap", page.url)
        for page in pages
        if _ok_html(page)
        and page.is_html
        and page.is_indexable
        and not page.in_sitemap
        and (page.canonical_url or page.header_canonical_url or page.url) == page.url
    ]


def sort_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Severity first, so truncation drops info rows and never critical ones."""
    return sorted(
        issues,
        key=lambda row: (SEVERITY_ORDER.get(row["severity"], 9), row["issue_type"]),
    )
