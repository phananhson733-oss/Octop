"""Octop's checks beyond upstream: path clusters, JSON-LD, head breaks, hreflang, sitemaps."""

from dataclasses import replace
from typing import Any

import pytest

from octop.infra.seo.audit.analyzer import analyze_html
from octop.infra.seo.audit.rules import (
    PageFacts,
    find_canonical_problems,
    find_hreflang_problems,
    find_sitemap_problems,
    page_issues,
)
from octop.infra.seo.audit.structured import (
    faq_questions,
    hidden_questions,
    missing_required,
    parse_blocks,
)
from octop.infra.seo.audit.urls import path_cluster

URL = "https://example.com/a"
HEALTHY = PageFacts(
    url=URL,
    status_code=200,
    fetch_class="ok",
    title="A perfectly reasonable page title",
    meta_description="A reasonable meta description that says something useful about the page.",
    h1_count=1,
    h1s=["A heading"],
    heading_order=[1, 2],
    word_count=500,
    is_html=True,
    link_count=1,
    is_indexable=True,
    response_time_ms=200,
    crawl_depth=1,
)


def _page(**overrides: Any) -> PageFacts:
    return replace(HEALTHY, **overrides)


def _found(page: PageFacts) -> dict[str, Any]:
    return {issue.issue_type: issue.details for issue in page_issues(page)}


@pytest.mark.parametrize(
    ("url", "cluster"),
    [
        ("https://example.com/", "/"),
        ("https://example.com/en", "/"),
        ("https://example.com/about", "/about"),
        ("https://example.com/balance-bike-size-chart/", "/balance-bike-size-chart"),
        ("https://example.com/products/bikes/strider-12", "/products/*/*"),
        ("https://example.com/zh-cn/products/bikes/x", "/products/*/*"),
        ("https://example.com/blog/post?page=2", "/blog/*?"),
    ],
)
def test_path_cluster(url: str, cluster: str) -> None:
    assert path_cluster(url) == cluster


def test_structured_data_requirements() -> None:
    documents, invalid = parse_blocks(
        [
            '{"@context": "https://schema.org", "@type": "Product", "name": "Bike"}',
            "<!-- " + '{"@type": "BreadcrumbList", "itemListElement": [{"@type": "ListItem",'
            ' "position": 1, "name": "Home"}]}' + " -->",
            '{"@type": "Product", "name": "Trailing", }',
            # A reference to a node defined elsewhere is not a second, empty product.
            '{"@graph": [{"@type": "Product", "@id": "#p", "name": "Car",'
            ' "offers": {"@type": "Offer", "price": "9"}}, {"@type": "Product", "@id": "#p"}]}',
        ]
    )
    assert invalid == 1
    assert missing_required(documents) == ["Product: offers | review | aggregateRating"]


def test_faq_questions_must_be_visible() -> None:
    documents, _ = parse_blocks(
        [
            '{"@type": "FAQPage", "mainEntity": ['
            '{"@type": "Question", "name": "How tall is a 2-year-old?",'
            ' "acceptedAnswer": {"@type": "Answer", "text": "About 86 cm."}},'
            '{"@type": "Question", "name": "Is it safe?",'
            ' "acceptedAnswer": {"@type": "Answer", "text": "Yes."}}]}'
        ]
    )
    questions = faq_questions(documents)
    assert questions == ["How tall is a 2-year-old?", "Is it safe?"]
    visible = "FAQ — how TALL is a 2 year old? About 86 cm."
    assert hidden_questions(questions, visible) == ["Is it safe?"]


def test_analyzer_finds_head_tags_that_land_in_the_body() -> None:
    html = (
        "<html><head><title>T</title>"
        '<meta name="description" content="early">'
        '<img src="/pixel.gif">'
        '<link rel="canonical" href="/a"><meta name="robots" content="noindex">'
        '<script type="application/ld+json">{"@type": "Thing"}</script>'
        '<link rel="alternate" hreflang="fr" href="/fr/a">'
        '</head><body><img src="/b.png" width="10" height="10"><img src="/c.png"></body></html>'
    )
    analysis = analyze_html(html, URL)
    assert analysis.head_broken_by == "img"
    assert analysis.misplaced_head == ["canonical", "robots", "hreflang"]
    assert analysis.json_ld == ['{"@type": "Thing"}']
    assert analysis.alternates == [("fr", "/fr/a")]
    # The pixel and /c.png have no size; /b.png does.
    assert analysis.images_missing_size == 2


def test_a_clean_head_and_a_noscript_pixel_do_not_break_it() -> None:
    html = (
        '<html><head><meta charset="utf-8"><title>T</title>'
        '<noscript><img src="/pixel.gif"></noscript>'
        '<link rel="canonical" href="/a"></head><body><p>Hi</p></body></html>'
    )
    analysis = analyze_html(html, URL)
    assert (analysis.head_broken_by, analysis.misplaced_head) == (None, [])


@pytest.mark.parametrize(
    ("overrides", "issue_type", "details"),
    [
        (
            {"title": "Product Not Found (404) | Shop", "word_count": 40},
            "soft-404",
            {"title": "Product Not Found (404) | Shop", "word_count": 40},
        ),
        ({"h1s": ["页面不存在"], "word_count": 10}, "soft-404", None),
        ({"title": "Oops! Page not found – Shop", "word_count": 80}, "soft-404", None),
        ({"title": "404 - Not Found", "word_count": 5}, "soft-404", None),
        ({"h1s": ["抱歉，您访问的页面不存在"], "word_count": 20}, "soft-404", None),
        (
            {"misplaced_head": ["canonical"], "head_broken_by": "img"},
            "head-elements-in-body",
            {"head_ended_at": "img", "ignored": ["canonical"]},
        ),
        (
            {
                "alternates": [("en-UK", "https://example.com/uk/a"), ("fr_FR", URL)],
                "relative_alternates": 1,
            },
            "hreflang-invalid",
            {"problems": ["invalid code: en-UK", "invalid code: fr_FR", "relative URLs: 1"]},
        ),
        (
            {"alternates": [("fr", "https://example.com/fr/a")]},
            "hreflang-invalid",
            {"problems": ["no self-reference"]},
        ),
        ({"schema_invalid": 2}, "structured-data-invalid", {"invalid_blocks": 2}),
        (
            {"schema_missing": ["Product: name"]},
            "structured-data-missing-fields",
            {"missing": ["Product: name"]},
        ),
        ({"faq_hidden": ["Q?"]}, "faq-schema-not-visible", {"count": 1, "questions": ["Q?"]}),
        (
            {"nofollow_internal": ["https://example.com/b"]},
            "internal-nofollow",
            {"count": 1, "examples": ["https://example.com/b"]},
        ),
        (
            {"images_missing_size": 3, "images_total": 4},
            "images-missing-size",
            {"images_missing_size": 3, "images_total": 4},
        ),
    ],
)
def test_page_checks(overrides: dict[str, Any], issue_type: str, details: Any) -> None:
    found = _found(_page(**overrides))
    assert issue_type in found
    if details is not None:
        assert found[issue_type] == details


@pytest.mark.parametrize(
    "overrides",
    [
        # A long page about 404 errors is not a soft 404.
        {"title": "How to fix 404 not found errors", "word_count": 1500},
        # Nor are short pages that only mention the words.
        {"title": "What is a 404 error?", "word_count": 250},
        {
            "title": "Lost and Not Found | ShortsBinge",
            "h1s": ["世界上不存在的恋人"],
            "word_count": 250,
        },
        {"alternates": [("en-GB", URL), ("zh-Hans", "https://example.com/zh/a")]},
        {"alternates": [("x-default", URL)]},
        # A body cut at the size limit may end inside a JSON-LD block.
        {"schema_invalid": 1, "body_truncated": True},
    ],
)
def test_page_checks_leave_healthy_pages_alone(overrides: dict[str, Any]) -> None:
    assert _found(_page(**overrides)) == {}


def _site(*pages: PageFacts) -> dict[str, PageFacts]:
    return {page.url: page for page in pages}


def test_canonical_targets_must_be_usable() -> None:
    pages = _site(
        _page(url="https://example.com/to-gone", canonical_url="https://example.com/gone"),
        _page(url="https://example.com/to-chain", canonical_url="https://example.com/chain"),
        _page(url="https://example.com/to-good", canonical_url="https://example.com/good"),
        _page(url="https://example.com/to-unseen", canonical_url="https://example.com/unseen"),
        _page(url="https://example.com/gone", status_code=404, is_html=False),
        _page(url="https://example.com/chain", canonical_url="https://example.com/good"),
        _page(url="https://example.com/good"),
    )
    issues = find_canonical_problems(pages)
    assert [(issue.page_url, issue.details) for issue in issues] == [
        (
            "https://example.com/to-gone",
            {"canonical_url": "https://example.com/gone", "reason": "error", "target_status": 404},
        ),
        (
            "https://example.com/to-chain",
            {"canonical_url": "https://example.com/chain", "reason": "chain", "target_status": 200},
        ),
    ]


def test_sitemap_urls_must_be_final_and_indexable() -> None:
    pages = [
        _page(url="https://example.com/ok", in_sitemap=True),
        _page(
            url="https://example.com/moved",
            in_sitemap=True,
            status_code=301,
            redirect_url="https://example.com/ok",
        ),
        _page(url="https://example.com/hidden", in_sitemap=True, is_indexable=False),
        _page(url="https://example.com/walled", in_sitemap=True, fetch_class="blocked"),
        _page(url="https://example.com/unlisted", is_indexable=False),
    ]
    issues = find_sitemap_problems(pages)
    assert [(issue.page_url, issue.details["reason"]) for issue in issues] == [
        ("https://example.com/moved", "redirect"),
        ("https://example.com/hidden", "noindex"),
    ]


def test_hreflang_pairs_must_be_reciprocal_and_usable() -> None:
    en, fr, de = (f"https://example.com/{lang}/a" for lang in ("en", "fr", "de"))
    pages = _site(
        _page(url=en, alternates=[("en", en), ("fr", fr), ("de", de)]),
        _page(url=fr, alternates=[("fr", fr)]),
        _page(url=de, is_indexable=False),
    )
    found = [
        (issue.issue_type, issue.page_url, issue.details) for issue in find_hreflang_problems(pages)
    ]
    assert found == [
        ("hreflang-no-return-link", en, {"hreflang": "fr", "target_url": fr}),
        (
            "hreflang-target-invalid",
            en,
            {"hreflang": "de", "target_url": de, "reason": "noindex", "target_status": 200},
        ),
    ]
