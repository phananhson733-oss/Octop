"""Rendered pages compared with their HTML, and the guard on the browser's requests."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from octop.infra.seo.audit import render as render_module
from octop.infra.seo.audit.render import (
    MAX_REQUESTS_PER_PAGE,
    ChromiumRenderer,
    Rendered,
    RenderUnavailable,
    _HostGuard,
    open_chromium_renderer,
    render_record,
)
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


def test_a_page_that_sends_the_browser_elsewhere_is_not_compared() -> None:
    elsewhere = "https://example.com/en/products/bikes/strider"
    rendered = Rendered(
        status=200,
        final_url=f"{elsewhere}#top",
        title="Another page",
        links=["https://example.com/en/a", "https://example.com/en/b", "https://example.com/en/c"],
    )
    record = render_record(rendered, URL, set())
    assert record["final_url"] == elsewhere
    # Its links are the other page's: no rendered-only diff, so no link checks either.
    assert "rendered_only" not in record
    assert _found(record) == {"render-redirected": {"final_url": elsewhere}}


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


class _Route:
    def __init__(self, url: str, resource_type: str = "fetch") -> None:
        self.request = SimpleNamespace(url=url, resource_type=resource_type, headers={})
        self.outcome: str | None = None

    async def abort(self) -> None:
        self.outcome = "aborted"

    async def continue_(self, headers: dict[str, str] | None = None) -> None:
        self.outcome = "continued"


class _Context:
    """A Playwright context whose page load sends ``routes`` through the route handler."""

    def __init__(self, routes: list[_Route], crash: Exception | None = None) -> None:
        self.routes = routes
        self.crash = crash
        self.handler: Any = None
        self.closed = False

    async def route(self, _pattern: str, handler: Any) -> None:
        self.handler = handler

    async def add_init_script(self, _script: str) -> None:
        pass

    async def new_page(self) -> Any:
        context = self

        class _Page:
            url = URL

            async def goto(self, _url: str, **_kwargs: Any) -> Any:
                for route in context.routes:
                    await context.handler(route)
                if context.crash is not None:
                    raise context.crash
                return SimpleNamespace(status=200)

            async def wait_for_load_state(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            async def wait_for_timeout(self, _ms: int) -> None:
                pass

            async def evaluate(self, _script: str, _details: bool) -> dict[str, Any]:
                return {"title": "Product Not Found (404)", "robots": "noindex", "h1s": []}

        return _Page()

    async def close(self) -> None:
        if self.crash is not None:
            raise RuntimeError("Target page, context or browser has been closed")
        self.closed = True


def _renderer(context: _Context) -> ChromiumRenderer:
    async def new_context(**_kwargs: Any) -> _Context:
        return context

    async def resolve(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    browser = SimpleNamespace(version="130.0", new_context=new_context)
    return ChromiumRenderer(browser, "example.com", resolve)


async def test_requests_the_browser_skips_do_not_count_toward_the_cap() -> None:
    images = [
        _Route(f"https://example.com/i/{i}.jpg", "image") for i in range(MAX_REQUESTS_PER_PAGE)
    ]
    data = _Route("https://example.com/api/product")
    rendered = await _renderer(_Context([*images, data])).render(URL)
    assert rendered.error is None
    assert data.outcome == "continued"
    assert {route.outcome for route in images} == {"aborted"}


async def test_a_page_cut_off_at_the_request_cap_is_not_compared() -> None:
    routes = [_Route(f"https://example.com/api/{i}") for i in range(MAX_REQUESTS_PER_PAGE + 1)]
    rendered = await _renderer(_Context(routes)).render(URL)
    assert (routes[-2].outcome, routes[-1].outcome) == ("continued", "aborted")
    assert rendered.error is not None and f"{MAX_REQUESTS_PER_PAGE} requests" in rendered.error
    # The "not found" state it shows came from refused requests: a failed render, no more.
    issues = find_render_problems({URL: HTML}, {URL: render_record(rendered, URL, set())})
    assert [issue.issue_type for issue in issues] == ["render-failed"]


async def test_a_browser_crash_fails_the_page_not_the_audit() -> None:
    rendered = await _renderer(_Context([], crash=RuntimeError("Target crashed"))).render(URL)
    assert rendered.error == "Target crashed"


async def test_the_process_runs_one_browser_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(render_module, "_BROWSER_SLOT", asyncio.Lock())
    running = 0
    most = 0
    done = asyncio.Event()

    @asynccontextmanager
    async def started(_url: str, _resolver: Any) -> AsyncIterator[Any]:
        nonlocal running, most
        running += 1
        most = max(most, running)
        try:
            yield SimpleNamespace()
        finally:
            running -= 1

    monkeypatch.setattr(render_module, "_started_renderer", started)

    async def audit() -> None:
        async with open_chromium_renderer(URL):
            await done.wait()

    first = asyncio.create_task(audit())
    await asyncio.sleep(0)
    # An inspection does not wait out an audit's whole render phase.
    with pytest.raises(RenderUnavailable, match="busy"):
        async with open_chromium_renderer(URL, wait_s=0.01):
            pass
    # Another audit waits its turn.
    second = asyncio.create_task(audit())
    await asyncio.sleep(0.01)
    done.set()
    await asyncio.gather(first, second)
    assert most == 1

    @asynccontextmanager
    async def missing(_url: str, _resolver: Any) -> AsyncIterator[Any]:
        raise RenderUnavailable("Executable doesn't exist")
        yield  # pragma: no cover

    monkeypatch.setattr(render_module, "_started_renderer", missing)
    with pytest.raises(RenderUnavailable, match="Executable"):
        async with open_chromium_renderer(URL):
            pass
    # A browser that never started gives the slot back.
    assert not render_module._BROWSER_SLOT.locked()
