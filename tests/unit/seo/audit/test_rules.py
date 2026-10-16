"""Issue rules, ported from OpenSEO page-reporters.test.ts and multipage.test.ts."""

from dataclasses import replace
from typing import Any

import pytest

from octop.infra.seo.audit.rules import (
    ISSUE_SEVERITY,
    PageFacts,
    find_broken_links,
    find_duplicates,
    find_orphans,
    find_redirect_chains_and_loops,
    page_issues,
)

HEALTHY = PageFacts(
    url="https://example.com/a",
    status_code=200,
    fetch_class="ok",
    title="A perfectly reasonable page title",
    meta_description="A reasonable meta description that says something useful about the page.",
    h1_count=1,
    heading_order=[1, 2, 3],
    word_count=500,
    content_hash="abc123",
    is_html=True,
    link_count=1,
    is_indexable=True,
    response_time_ms=200,
    crawl_depth=1,
)


def _page(**overrides: Any) -> PageFacts:
    return replace(HEALTHY, **overrides)


def _types(page: PageFacts) -> list[str]:
    return [issue.issue_type for issue in page_issues(page)]


def test_every_issue_type_has_copy() -> None:
    import json
    from pathlib import Path

    import octop.i18n

    root = Path(octop.i18n.__file__).parent
    for locale in ("en", "zh"):
        issues = json.loads((root / f"{locale}.json").read_text(encoding="utf-8"))["seo"]["audit"][
            "issues"
        ]
        assert list(issues) == list(ISSUE_SEVERITY)
        assert all(set(copy) == {"title", "explanation", "how_to_fix"} for copy in issues.values())


def test_unread_shell_keeps_known_signals_without_inventing_content_issues() -> None:
    page = _page(
        javascript_shell=True,
        title="",
        meta_description="",
        h1_count=0,
        word_count=1,
        link_count=0,
        is_indexable=False,
        robots_meta="noindex",
        canonical_url="https://example.com/canonical",
        header_canonical_url="https://example.com/header-canonical",
        response_time_ms=2000,
        crawl_depth=5,
    )
    assert _types(page) == [
        "slow-response",
        "noindex-page",
        "canonical-conflict",
        "canonicalized-page",
        "deep-page",
        "javascript-rendering-suspected",
    ]


def test_healthy_page_reports_nothing() -> None:
    assert _types(HEALTHY) == []


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"fetch_class": "blocked", "status_code": 403}, ["blocked-page"]),
        ({"fetch_class": "rate_limited", "status_code": 429}, ["rate-limited-page"]),
        ({"fetch_class": "error", "status_code": 0}, []),
        ({"status_code": 500}, ["server-error"]),
        ({"status_code": 404}, ["broken-page"]),
        ({"status_code": 301, "redirect_url": "https://example.com/b"}, []),
    ],
)
def test_fetch_outcomes(overrides: dict[str, Any], expected: list[str]) -> None:
    assert _types(_page(**overrides)) == expected


@pytest.mark.parametrize(
    ("overrides", "issue_type"),
    [
        ({"title": ""}, "missing-title"),
        ({"title": "x" * 70}, "title-too-long"),
        ({"title": "Tiny"}, "title-too-short"),
        ({"meta_description": ""}, "missing-meta-description"),
        ({"meta_description": "x" * 200}, "meta-description-too-long"),
        ({"meta_description": "x" * 69}, "meta-description-too-short"),
        ({"h1_count": 0}, "missing-h1"),
        ({"h1_count": 3}, "multiple-h1"),
        ({"heading_order": [1, 2, 4]}, "heading-order-skip"),
        ({"word_count": 50}, "thin-content"),
        ({"response_time_ms": 3000}, "slow-response"),
        ({"crawl_depth": 6}, "deep-page"),
        ({"link_count": 0}, "no-outgoing-links"),
        ({"images_missing_alt": 2, "images_total": 3}, "images-missing-alt"),
    ],
)
def test_flags(overrides: dict[str, Any], issue_type: str) -> None:
    assert issue_type in _types(_page(**overrides))


@pytest.mark.parametrize(
    ("overrides", "issue_type"),
    [
        ({"meta_description": "x" * 70}, "meta-description-too-short"),
        ({"word_count": 50, "is_indexable": False, "robots_meta": "noindex"}, "thin-content"),
        ({"crawl_depth": None}, "deep-page"),
        ({"link_count": 0, "is_indexable": False}, "no-outgoing-links"),
    ],
)
def test_does_not_flag(overrides: dict[str, Any], issue_type: str) -> None:
    assert issue_type not in _types(_page(**overrides))


def test_short_meta_description_reports_its_length() -> None:
    issues = page_issues(_page(meta_description="x" * 69))
    assert next(i for i in issues if i.issue_type == "meta-description-too-short").details == {
        "length": 69
    }


@pytest.mark.parametrize(
    ("is_html", "expected"),
    [
        (False, []),
        (True, ["missing-title", "missing-meta-description", "missing-h1", "thin-content"]),
    ],
)
def test_empty_document_is_checked_only_as_html(is_html: bool, expected: list[str]) -> None:
    page = _page(
        is_html=is_html,
        title="",
        meta_description="",
        h1_count=0,
        heading_order=[],
        word_count=0,
        content_hash=None,
    )
    assert _types(page) == expected


def test_indexability_and_canonical_signals() -> None:
    assert "noindex-page" in _types(_page(is_indexable=False, robots_meta="noindex"))
    conflicted = _types(
        _page(
            canonical_url="https://example.com/canonical-a",
            header_canonical_url="https://example.com/canonical-b",
        )
    )
    assert {"canonical-conflict", "canonicalized-page"} <= set(conflicted)
    assert "canonicalized-page" not in _types(_page(canonical_url="https://example.com/a"))


def test_duplicate_titles_name_the_other_urls() -> None:
    issues = find_duplicates(
        [
            _page(url="https://example.com/a", title="Same"),
            _page(url="https://example.com/b", title="Same"),
            _page(url="https://example.com/c", title="Different"),
        ]
    )
    titles = [issue for issue in issues if issue.issue_type == "duplicate-title"]
    assert len(titles) == 2
    assert titles[0].details == {"group_size": 2, "other_urls": ["https://example.com/b"]}


def test_duplicates_leave_out_noindexed_canonicalized_and_blocked_pages() -> None:
    issues = find_duplicates(
        [
            _page(url="https://example.com/a", title="Same", meta_description="A"),
            _page(
                url="https://example.com/b",
                title="Same",
                meta_description="B",
                canonical_url="https://example.com/a",
            ),
            _page(
                url="https://example.com/c", title="Same", meta_description="C", is_indexable=False
            ),
            _page(
                url="https://example.com/d",
                title="Same",
                meta_description="D",
                fetch_class="blocked",
                status_code=403,
            ),
        ]
    )
    assert [i for i in issues if i.issue_type != "duplicate-content"] == []


def test_duplicate_content_needs_text() -> None:
    issues = find_duplicates(
        [
            _page(url="https://example.com/a", title="A", meta_description="A", content_hash="h1"),
            _page(url="https://example.com/b", title="B", meta_description="B", content_hash="h1"),
            _page(
                url="https://example.com/e1",
                title="E1",
                meta_description="E1",
                content_hash="h2",
                word_count=0,
            ),
            _page(
                url="https://example.com/e2",
                title="E2",
                meta_description="E2",
                content_hash="h2",
                word_count=0,
            ),
        ]
    )
    assert [i.page_url for i in issues if i.issue_type == "duplicate-content"] == [
        "https://example.com/a",
        "https://example.com/b",
    ]


def _redirect(url: str, target: str) -> PageFacts:
    return _page(url=url, status_code=301, redirect_url=target)


def test_single_redirects_are_fine() -> None:
    pages = [
        _redirect("https://example.com/a", "https://example.com/b"),
        _page(url="https://example.com/b"),
    ]
    assert find_redirect_chains_and_loops(pages) == []


def test_a_chain_is_flagged_once_on_its_head() -> None:
    issues = find_redirect_chains_and_loops(
        [
            _redirect("https://example.com/a", "https://example.com/b"),
            _redirect("https://example.com/b", "https://example.com/c"),
            _page(url="https://example.com/c"),
        ]
    )
    assert len(issues) == 1
    assert issues[0].issue_type == "redirect-chain"
    assert issues[0].page_url == "https://example.com/a"
    assert issues[0].details is not None
    assert issues[0].details["hops"] == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ]


@pytest.mark.parametrize(
    "pages",
    [
        [
            _redirect("https://example.com/a", "https://example.com/b"),
            _redirect("https://example.com/b", "https://example.com/a"),
        ],
        [_redirect("https://example.com/a", "https://example.com/a")],
    ],
    ids=["two-page loop", "self loop"],
)
def test_a_loop_is_flagged_once(pages: list[PageFacts]) -> None:
    issues = find_redirect_chains_and_loops(pages)
    assert [issue.issue_type for issue in issues] == ["redirect-loop"]


def test_broken_links_point_at_failed_pages_only() -> None:
    pages = {
        "https://example.com/": _page(url="https://example.com/"),
        "https://example.com/gone": _page(url="https://example.com/gone", status_code=404),
        "https://example.com/wall": _page(
            url="https://example.com/wall", status_code=403, fetch_class="blocked"
        ),
    }
    links = [
        ("https://example.com/", "https://example.com/gone"),
        ("https://example.com/", "https://example.com/wall"),
        ("https://example.com/", "https://example.com/never-crawled"),
    ]
    issues = find_broken_links(pages, links)
    assert [(i.page_url, i.details) for i in issues] == [
        (
            "https://example.com/",
            {"target_url": "https://example.com/gone", "target_status": 404},
        )
    ]


def test_orphans_skip_the_start_page_linked_pages_and_redirect_targets() -> None:
    pages = {
        url: _page(url=url)
        for url in (
            "https://example.com/",
            "https://example.com/linked",
            "https://example.com/self-linked",
            "https://example.com/target",
            "https://example.com/orphan",
        )
    }
    pages["https://example.com/old"] = _redirect(
        "https://example.com/old", "https://example.com/target"
    )
    links = [
        ("https://example.com/", "https://example.com/linked"),
        ("https://example.com/self-linked", "https://example.com/self-linked"),
    ]
    orphans = find_orphans(pages, links, "https://example.com/")
    assert [issue.page_url for issue in orphans] == [
        "https://example.com/self-linked",
        "https://example.com/orphan",
    ]
