"""Rendered pages compared with their HTML, and the guard on the browser's requests."""

from dataclasses import replace
from typing import Any

import pytest

from octop.infra.seo.audit.render import _HostGuard
from octop.infra.seo.audit.rules import PageFacts, find_render_problems

URL = "https://example.com/products/bikes/strider"
HTML = PageFacts(
    url=URL,
    status_code=200,
    fetch_class="ok",
    title="Strider 12 Sport balance bike",
    h1_count=1,
    h1s=["Strider 12 Sport"],
    word_count=400,
    is_html=True,
    is_indexable=True,
    canonical_url=URL,
)
SAME = {
    "status": 200,
    "title": "Strider 12 Sport balance bike",
    "robots": "",
    "canonical": URL,
    # Case and spacing differences are CSS and markup, not a different heading.
    "h1s": ["STRIDER  12 Sport"],
    "word_count": 450,
    "rendered_only": [],
    "rendered_only_count": 0,
    "link_status": {},
    "unlinked": 0,
    "trace": [],
}


def _found(render: dict[str, Any], page: PageFacts = HTML) -> dict[str, Any]:
    issues = find_render_problems({page.url: page}, {page.url: {**SAME, **render}})
    return {issue.issue_type: issue.details for issue in issues}


def test_an_unchanged_page_has_no_findings() -> None:
    assert _found({}) == {}


@pytest.mark.parametrize(
    ("render", "expected"),
    [
        (
            {"canonical": "https://example.com/products"},
            {
                "render-canonical-changed": {
                    "html_canonical": URL,
                    "rendered_canonical": "https://example.com/products",
                }
            },
        ),
        (
            {"h1s": []},
            {"render-h1-changed": {"html_h1s": ["Strider 12 Sport"], "rendered_h1s": []}},
        ),
        (
            {"title": "Shop", "trace": [{"t": 1700, "title": "Shop", "robots": ""}]},
            {
                "render-title-changed": {
                    "html_title": "Strider 12 Sport balance bike",
                    "rendered_title": "Shop",
                    "at_ms": 1700,
                    "still_shown_at_end": True,
                }
            },
        ),
        (
            # A head manager that shows another title for a while, then puts the right one back.
            {
                "trace": [
                    {"t": 9, "title": "Strider 12 Sport balance bike", "robots": ""},
                    {"t": 1665, "title": "Trusted reviews | Shop", "robots": ""},
                    {"t": 3654, "title": "Strider 12 Sport balance bike", "robots": ""},
                ]
            },
            {
                "render-title-changed": {
                    "html_title": "Strider 12 Sport balance bike",
                    "rendered_title": "Trusted reviews | Shop",
                    "at_ms": 1665,
                    "still_shown_at_end": False,
                }
            },
        ),
        (
            {"word_count": 600},
            {"render-content-missing-from-html": {"html_words": 400, "rendered_words": 600}},
        ),
        (
            {
                "rendered_only": ["https://example.com/x", "https://example.com/y"],
                "rendered_only_count": 3,
            },
            {
                "render-links-missing-from-html": {
                    "count": 3,
                    "examples": ["https://example.com/x", "https://example.com/y"],
                }
            },
        ),
        (
            {"status": 403},
            {"render-failed": {"error": None, "status_code": 403}},
        ),
        (
            # The final page says not found: the error state stays.
            {"title": "Product Not Found (404)", "robots": "noindex"},
            {
                "render-noindex": {"rendered_robots": "noindex"},
                "render-error-state": {
                    "title": "Product Not Found (404)",
                    "robots": "noindex",
                    "at_ms": None,
                    "still_shown_at_end": True,
                },
            },
        ),
        (
            {"trace": [{"t": 80, "title": "Strider 12 Sport balance bike", "robots": "noindex"}]},
            {
                "render-error-state": {
                    "title": "Strider 12 Sport balance bike",
                    "robots": "noindex",
                    "at_ms": 80,
                    "still_shown_at_end": False,
                }
            },
        ),
        (
            # What balancebiketoddler.com product pages did on 2026-10-16: the data fetch
            # shows "not found" and noindex for three seconds, then the real page again.
            {
                "trace": [
                    {"t": 9, "title": "Strider 12 Sport balance bike", "robots": "index,follow"},
                    {
                        "t": 1740,
                        "title": "Product Not Found (404) | Shop",
                        "robots": "noindex,nofollow",
                    },
                    {"t": 5309, "title": "Strider 12 Sport balance bike", "robots": "index,follow"},
                ]
            },
            {
                "render-error-state": {
                    "title": "Product Not Found (404) | Shop",
                    "robots": "noindex,nofollow",
                    "at_ms": 1740,
                    "still_shown_at_end": False,
                }
            },
        ),
    ],
)
def test_render_findings(render: dict[str, Any], expected: dict[str, Any]) -> None:
    assert _found(render) == expected


@pytest.mark.parametrize(
    "render",
    [
        # Two links added by script (cart, account) are not navigation.
        {"rendered_only": ["https://example.com/cart"], "rendered_only_count": 2},
        # The HTML's links were cut off, so no diff is made at all.
        {"rendered_only": None, "rendered_only_count": 9},
    ],
)
def test_render_findings_leave_normal_script_effects_alone(render: dict[str, Any]) -> None:
    assert _found(render) == {}


@pytest.mark.parametrize(
    ("html_words", "rendered_words"),
    # 2.5 times as many but only 150 more; 400 more but only 1.4 times as many.
    [(100, 250), (1000, 1400)],
)
def test_a_little_script_text_is_not_missing_content(html_words: int, rendered_words: int) -> None:
    assert _found({"word_count": rendered_words}, replace(HTML, word_count=html_words)) == {}


def test_a_page_that_is_a_not_found_page_in_its_html_is_left_to_soft_404() -> None:
    gone = PageFacts(
        url=URL,
        status_code=200,
        fetch_class="ok",
        title="Not found",
        h1s=["Not found"],
        is_html=True,
        is_indexable=True,
        canonical_url=URL,
        word_count=30,
    )
    render = {"title": "Not found", "h1s": ["Not found"], "word_count": 30}
    assert "render-error-state" not in _found(render, gone)


def test_links_found_only_after_rendering_are_checked() -> None:
    crawled_broken = "https://example.com/gone"
    checked_broken = "https://example.com/reviews/safety"
    fine = "https://example.com/guides"
    pages = {
        URL: HTML,
        crawled_broken: PageFacts(url=crawled_broken, status_code=404, fetch_class="ok"),
    }
    render = {
        **SAME,
        "rendered_only": [crawled_broken, checked_broken, fine],
        "rendered_only_count": 3,
        "link_status": {checked_broken: 404, fine: 200},
    }
    broken = [
        issue.details
        for issue in find_render_problems(pages, {URL: render})
        if issue.issue_type == "broken-internal-link"
    ]
    assert broken == [
        {"target_url": crawled_broken, "target_status": 404, "rendered_only": True},
        {"target_url": checked_broken, "target_status": 404, "rendered_only": True},
    ]


def test_an_unavailable_browser_is_reported_once() -> None:
    issues = find_render_problems(
        {URL: HTML}, {URL: {"error": "Executable doesn't exist", "unavailable": True}}
    )
    assert [(issue.issue_type, issue.details) for issue in issues] == [
        ("render-unavailable", {"error": "Executable doesn't exist"})
    ]


async def test_the_browser_may_only_reach_public_addresses() -> None:
    answers = {
        "example.com": ["93.184.216.34"],
        "mixed.example": ["93.184.216.34", "10.0.0.5"],
        "cdn.example": ["2606:4700::6810:84e5"],
    }
    lookups: list[str] = []

    async def resolve(host: str, _port: int) -> list[str]:
        lookups.append(host)
        if host not in answers:
            raise OSError("no such host")
        return answers[host]

    guard = _HostGuard(resolve)
    verdicts = {
        host: await guard.allows(host)
        for host in (
            "example.com",
            "EXAMPLE.com",
            "mixed.example",
            "cdn.example",
            "missing.example",
            "localhost",
            "127.0.0.1",
            "169.254.169.254",
            "93.184.216.34",
        )
    }
    assert verdicts == {
        "example.com": True,
        "EXAMPLE.com": True,
        "mixed.example": False,
        "cdn.example": True,
        "missing.example": False,
        "localhost": False,
        "127.0.0.1": False,
        "169.254.169.254": False,
        "93.184.216.34": True,
    }
    # One lookup per host, and none for IP literals or internal names.
    assert lookups == ["example.com", "mixed.example", "cdn.example", "missing.example"]
