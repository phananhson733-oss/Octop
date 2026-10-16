"""The expert's live look at one page: served HTML, rendered page, content signals."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from octop.infra.errors import OctopError
from octop.infra.seo.audit import service as service_module
from octop.infra.seo.audit.inspect import content_signals, inspect_page
from octop.infra.seo.audit.render import Rendered, RenderUnavailable
from octop.infra.seo.audit.service import SeoAuditService

from .fakesite import FakeSite, html, redirect

ORIGIN = "https://example.com"
PRODUCT = f"{ORIGIN}/products/bikes/strider"
JSON_LD = '{"@type": "Product", "name": "Strider 12"}'
BODY = (
    "<h2>Sizing</h2><p>A 5 min read. We tested it for six weeks. Seat height 28-48 cm, "
    "fits riders 18 months to 5 years, weighs 6.7 lbs.</p>"
)


def _site() -> FakeSite:
    return FakeSite(
        {
            f"{ORIGIN}/old": redirect("/products/bikes/strider"),
            PRODUCT: html(
                "<html><head><title>Strider 12 Sport review</title>"
                '<link rel="canonical" href="/products/bikes/strider">'
                f'<script type="application/ld+json">{JSON_LD}</script></head>'
                f"<body><h1>Strider 12 Sport</h1>{BODY}<a href='/'>Home</a></body></html>"
            ),
            f"{ORIGIN}/away": redirect("https://other.example/x"),
            f"{ORIGIN}/feed.xml": httpx.Response(
                200, headers={"content-type": "application/xml"}, text="<rss/>"
            ),
        }
    )


class _Browser:
    def __init__(self, rendered: Rendered) -> None:
        self.rendered = rendered
        self.calls: list[tuple[str, bool]] = []

    @asynccontextmanager
    async def open(self, _url: str) -> AsyncIterator["_Browser"]:
        yield self

    async def render(self, url: str, *, details: bool = False) -> Rendered:
        self.calls.append((url, details))
        return self.rendered


LOADING_404 = Rendered(
    status=200,
    title="Strider 12 Sport review",
    canonical=PRODUCT,
    h1s=["Strider 12 Sport"],
    word_count=60,
    links=[f"{ORIGIN}/"],
    trace=[
        {"t": 5, "title": "Strider 12 Sport review", "robots": "index"},
        {"t": 1600, "title": "Product Not Found (404)", "robots": "noindex,nofollow"},
        {"t": 4500, "title": "Strider 12 Sport review", "robots": "index"},
    ],
    outline=[[1, "Strider 12 Sport"], [2, "Sizing"]],
    main_text="Strider 12 Sport. A 3 min read. Seat height 30-50 cm.",
)


async def test_inspect_follows_redirects_and_compares_the_rendered_page() -> None:
    site = _site()
    browser = _Browser(LOADING_404)
    result = await inspect_page(
        f"{ORIGIN}/old", client_factory=site.client, renderer_factory=browser.open
    )
    assert result["final_url"] == PRODUCT
    assert [(hop["url"], hop["status_code"]) for hop in result["redirects"]] == [
        (f"{ORIGIN}/old", 301),
        (PRODUCT, 200),
    ]
    page = result["html"]
    assert (page["title"], page["canonical_url"], page["h1s"]) == (
        "Strider 12 Sport review",
        PRODUCT,
        ["Strider 12 Sport"],
    )
    assert page["json_ld"] == [JSON_LD]
    assert page["structured_data"]["missing_required"] == [
        "Product: offers | review | aggregateRating"
    ]
    assert "We tested it for six weeks" in page["text"]
    # The browser opened the final URL, asking for the outline and main text.
    assert browser.calls == [(PRODUCT, True)]
    assert result["rendered"]["outline"] == [[1, "Strider 12 Sport"], [2, "Sizing"]]
    assert result["render_findings"] == [
        {
            "issue_type": "render-error-state",
            "severity": "critical",
            "details": {
                "title": "Product Not Found (404)",
                "robots": "noindex,nofollow",
                "at_ms": 1600,
                "still_shown_at_end": False,
            },
        }
    ]
    # Signals read the rendered main text, what a visitor sees.
    assert result["content_signals"]["reading_time_claims"] == [
        "Strider 12 Sport. A 3 min read. Seat height 30-50 cm."
    ]
    assert "never follow" in result["notice"]


async def test_inspect_without_a_browser_still_reads_the_html() -> None:
    @asynccontextmanager
    async def no_browser(_url: str) -> AsyncIterator[_Browser]:
        raise RenderUnavailable("Executable doesn't exist")
        yield  # pragma: no cover

    result = await inspect_page(PRODUCT, client_factory=_site().client, renderer_factory=no_browser)
    assert result["rendered"] == {"unavailable": True, "error": "Executable doesn't exist"}
    assert result["render_findings"] == []
    assert result["html"]["title"] == "Strider 12 Sport review"
    assert result["content_signals"]["testing_claims"]


async def test_inspect_skips_rendering_when_asked() -> None:
    browser = _Browser(LOADING_404)
    result = await inspect_page(
        PRODUCT, render=False, client_factory=_site().client, renderer_factory=browser.open
    )
    assert (result["rendered"], browser.calls) == (None, [])
    assert "render_findings" not in result


@pytest.mark.parametrize(
    ("path", "final", "status"),
    [
        # The site header must not follow a redirect off the site.
        ("/away", "/away", 301),
        ("/feed.xml", "/feed.xml", 200),
        ("/missing", "/missing", 404),
    ],
)
async def test_inspect_reads_only_html_pages_of_the_site(
    path: str, final: str, status: int
) -> None:
    site = _site()
    browser = _Browser(LOADING_404)
    result = await inspect_page(
        f"{ORIGIN}{path}", client_factory=site.client, renderer_factory=browser.open
    )
    assert (result["final_url"], result["status_code"]) == (f"{ORIGIN}{final}", status)
    assert (result["html"], result["rendered"], browser.calls) == (None, None, [])
    assert not any("other.example" in request for request in site.requests)


def test_content_signals() -> None:
    signals = content_signals(
        "Reading time: 12 min. We measured the frame: 30 cm, then 32 cm. "
        "Hands-on notes follow. Suits ages 2-5 years. Ranked #3,070 in Toys."
    )
    assert signals["words"] == 24
    assert signals["estimated_reading_minutes"] == 0.1
    assert len(signals["reading_time_claims"]) == 1
    assert [snippet.count("cm") for snippet in signals["measurements"]][:1] == [2]
    # 30 cm, 32 cm and 2-5 years; "#3,070 in Toys" is not a length.
    assert len(signals["measurements"]) == 3
    assert len(signals["testing_claims"]) == 2


@pytest.fixture
async def project(context: Any) -> tuple[Any, Any, int, str]:
    service, repos, user, _other = context
    created = await service.create_project(user, name="Site", domain="example.com", agent_id="seo")
    return service, repos, user, created["project_id"]


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://other.example/products/x", "other_site"),
        ("http://127.0.0.1/admin", "not_public"),
        ("ftp://example.com/x", "invalid_url"),
    ],
)
async def test_inspect_is_limited_to_the_projects_own_site(
    project: Any, url: str, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _repos, user, project_id = project
    fetched: list[str] = []

    async def fake_inspect(target: str, **_kwargs: Any) -> dict[str, Any]:
        fetched.append(target)
        return {}

    monkeypatch.setattr(service_module, "inspect_page", fake_inspect)
    audits = SeoAuditService(service)
    with pytest.raises(OctopError) as rejected:
        await audits.inspect(user, project_id, url, locale="en")
    assert rejected.value.code.value == "SEO_INVALID_INPUT"
    assert rejected.value.details["reason"]
    assert fetched == []
    # www and the bare domain are the same site.
    await audits.inspect(user, project_id, "www.example.com/products/x")
    assert fetched == ["https://www.example.com/products/x"]


async def test_the_inspect_tool_fits_a_huge_page(
    project: Any, context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from octop.infra.seo.tools import MAX_RESULT_CHARS, build_seo_tools

    service, _repos, user, _project_id = project
    huge = "word " * 40_000
    site = FakeSite(
        {
            PRODUCT: html(
                f"<html><head><title>Huge page</title></head><body><main>{huge}</main></body></html>"
            )
        }
    )
    browser = _Browser(Rendered(status=200, title="Huge page", word_count=40_000, main_text=huge))
    monkeypatch.setattr(
        service_module,
        "inspect_page",
        partial(inspect_page, client_factory=site.client, renderer_factory=browser.open),
    )
    configurable = {"user": str(user), "agent_id": "seo", "thread_id": "thread-a"}
    monkeypatch.setattr("octop.infra.seo.tools.get_config", lambda: {"configurable": configurable})
    threads = Mock()
    threads.get.return_value = SimpleNamespace(user_id=user, agent_id="seo")
    tools = {tool.name: tool for tool in build_seo_tools(service, "seo", threads)}

    text = await tools["seo_inspect_page"].ainvoke({"url": PRODUCT})
    assert len(text) <= MAX_RESULT_CHARS
    result = json.loads(text)
    assert result["html"]["text_truncated"] and result["rendered"]["main_text_truncated"]
    assert result["html"]["title"] == "Huge page"
