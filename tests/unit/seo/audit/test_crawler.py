import httpx
import pytest

from octop.infra.seo.audit.crawler import (
    crawl_page,
    discover_sitemap_urls,
    parse_sitemap,
    resolve_start_url,
    seeds_for,
)
from octop.infra.seo.audit.net import USER_AGENT
from octop.infra.seo.audit.robots import parse_robots
from octop.infra.seo.audit.throttle import CrawlThrottle
from octop.infra.seo.audit.urls import TargetBlocked

from .fakesite import FakeSite, html, page, redirect

ORIGIN = "https://example.com"


def _urlset(*urls: str) -> bytes:
    entries = "".join(f"<url><loc>{url}</loc></url>" for url in urls)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>'
    ).encode()


def _xml(body: bytes) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "application/xml"}, content=body)


class _NoWait:
    now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _throttle() -> CrawlThrottle:
    fake = _NoWait()
    return CrawlThrottle(fake.clock, fake.sleep)


def test_parse_sitemap_reads_urlsets_and_indexes() -> None:
    assert parse_sitemap(_urlset("https://example.com/a", "/b"), ORIGIN) == (
        [],
        ["https://example.com/a", "https://example.com/b"],
    )
    index = (
        b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<sitemap><loc>https://example.com/posts.xml</loc></sitemap></sitemapindex>"
    )
    assert parse_sitemap(index, ORIGIN) == (["https://example.com/posts.xml"], [])


@pytest.mark.parametrize(
    "body",
    [
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><urlset><url><loc>&a;</loc></url></urlset>',
        b"<urlset><url><loc>https://example.com/a</loc></url>",
        b"not xml",
    ],
    ids=["dtd", "truncated", "garbage"],
)
def test_parse_sitemap_refuses_unsafe_or_broken_documents(body: bytes) -> None:
    assert parse_sitemap(body, ORIGIN) == ([], [])


async def test_discovery_keeps_sitemap_urls_past_the_page_budget() -> None:
    posts = [f"https://example.com/blog/post-{i}/" for i in range(5)]
    site = FakeSite(
        {
            f"{ORIGIN}/robots.txt": httpx.Response(
                200, text=f"User-agent: *\nAllow: /\nSitemap: {ORIGIN}/index.xml"
            ),
            f"{ORIGIN}/index.xml": _xml(
                b"<sitemapindex><sitemap><loc>https://example.com/posts.xml</loc></sitemap>"
                b"<sitemap><loc>https://elsewhere.example/x.xml</loc></sitemap></sitemapindex>"
            ),
            f"{ORIGIN}/posts.xml": _xml(_urlset(*posts, f"{ORIGIN}/", "https://other.example/")),
        }
    )
    robots = parse_robots(ORIGIN, f"Sitemap: {ORIGIN}/index.xml")
    async with site.client() as client:
        urls, truncated = await discover_sitemap_urls(client, ORIGIN, robots, max_pages=2)
    assert urls == [*posts, f"{ORIGIN}/"]
    assert truncated is False
    assert not any("elsewhere" in request for request in site.requests)


def _index(*sitemaps: str) -> httpx.Response:
    entries = "".join(f"<sitemap><loc>{ORIGIN}{path}</loc></sitemap>" for path in sitemaps)
    return _xml(f"<sitemapindex>{entries}</sitemapindex>".encode())


@pytest.mark.parametrize(
    "routes",
    [
        # More URLs than discovery keeps (500 at the smallest budget).
        {"/big.xml": _xml(_urlset(*(f"{ORIGIN}/p/{i}" for i in range(501))))},
        # An index nested deeper than three levels.
        {
            "/big.xml": _index("/l2.xml"),
            "/l2.xml": _index("/l3.xml"),
            "/l3.xml": _index("/l4.xml"),
            "/l4.xml": _xml(_urlset(f"{ORIGIN}/deep")),
        },
    ],
)
async def test_discovery_says_when_it_stopped_short(routes: dict[str, httpx.Response]) -> None:
    site = FakeSite({f"{ORIGIN}{path}": response for path, response in routes.items()})
    robots = parse_robots(ORIGIN, f"Sitemap: {ORIGIN}/big.xml")
    async with site.client() as client:
        urls, truncated = await discover_sitemap_urls(client, ORIGIN, robots, max_pages=2)
    assert truncated is True
    assert f"{ORIGIN}/deep" not in urls and len(urls) <= 500


def test_seeds_follow_robots() -> None:
    robots = parse_robots(ORIGIN, "User-agent: *\nDisallow: /private")
    start, seeds = seeds_for(
        f"{ORIGIN}/",
        ORIGIN,
        robots,
        [f"{ORIGIN}/a", f"{ORIGIN}/private/x", "https://other.example/", f"{ORIGIN}/a"],
        USER_AGENT,
    )
    assert start == f"{ORIGIN}/"
    assert seeds == [f"{ORIGIN}/a"]


async def test_crawl_page_analyzes_html() -> None:
    site = FakeSite({f"{ORIGIN}/": page("Home", "/about", "https://other.example/")})
    async with site.client() as client:
        crawled = await crawl_page(client, f"{ORIGIN}/", 0, True, _throttle())
    assert crawled is not None
    assert (crawled.status_code, crawled.fetch_class, crawled.is_html) == (200, "ok", True)
    assert crawled.title == "Home page title"
    assert [(link.target_url, link.is_internal) for link in crawled.links] == [
        (f"{ORIGIN}/about", True),
        ("https://other.example/", False),
    ]
    assert crawled.is_indexable and crawled.in_sitemap and crawled.crawl_depth == 0


async def test_crawl_page_records_what_the_head_leaves_out() -> None:
    bare = html(
        "<html><head><title>T</title></head><body>"
        '<img src="/hero.jpg" loading="lazy"><img src="/b.jpg"><p>Text</p></body></html>'
    )
    full = html(
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width">'
        '<link rel="shortcut icon" href="/favicon.ico">'
        '<meta property="og:title" content="T"><meta property="og:description" content="D">'
        '<meta property="og:image" content="/og.png"><title>T</title></head><body>'
        '<nav itemscope itemtype="https://schema.org/BreadcrumbList"></nav>'
        '<script type="application/ld+json">{"@graph": [{"@type": "Article"}]}</script>'
        '<img src="/hero.jpg"><img src="/b.jpg" loading="lazy"></body></html>',
    )
    site = FakeSite({f"{ORIGIN}/bare": bare, f"{ORIGIN}/full": full})
    async with site.client() as client:
        first = await crawl_page(client, f"{ORIGIN}/bare", 1, False, _throttle())
        second = await crawl_page(client, f"{ORIGIN}/full", 1, False, _throttle())
    assert first is not None and second is not None
    # The response header declares the charset for the bare page.
    assert first.missing_declarations == ["viewport", "lang", "icon"]
    assert first.missing_open_graph == ["og:title", "og:description", "og:image"]
    assert (first.lazy_first_image, first.structured_types) == (f"{ORIGIN}/hero.jpg", [])
    assert (second.missing_declarations, second.missing_open_graph) == ([], [])
    assert second.lazy_first_image is None
    assert second.structured_types == ["Article", "BreadcrumbList"]


async def test_crawl_page_records_redirects_without_following() -> None:
    site = FakeSite({f"{ORIGIN}/old": redirect("/new?b=2&a=1")})
    async with site.client() as client:
        crawled = await crawl_page(client, f"{ORIGIN}/old", 1, False, _throttle())
    assert crawled is not None
    assert (crawled.status_code, crawled.redirect_url) == (301, f"{ORIGIN}/new?a=1&b=2")
    assert site.requests == [f"GET {ORIGIN}/old"]


async def test_crawl_page_retries_a_429() -> None:
    answers = iter([httpx.Response(429, headers={"retry-after": "2"}), page("Home")])
    site = FakeSite({f"{ORIGIN}/": lambda _request: next(answers)})
    async with site.client() as client:
        crawled = await crawl_page(client, f"{ORIGIN}/", 0, False, _throttle())
    assert crawled is not None
    assert (crawled.status_code, crawled.rate_limited) == (200, True)


async def test_crawl_page_treats_a_vercel_challenge_as_blocked() -> None:
    site = FakeSite(
        {f"{ORIGIN}/": httpx.Response(429, headers={"x-vercel-mitigated": "challenge"})}
    )
    throttle = _throttle()
    async with site.client() as client:
        crawled = await crawl_page(client, f"{ORIGIN}/", 0, False, throttle)
    assert crawled is not None
    assert (crawled.status_code, crawled.fetch_class, crawled.rate_limited) == (
        429,
        "blocked",
        False,
    )
    assert site.requests == [f"GET {ORIGIN}/"]
    assert (throttle.consecutive_rate_limits, throttle.cooldown) == (0, 0.0)


async def test_crawl_page_classifies_blocks_and_failures() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    site = FakeSite(
        {
            f"{ORIGIN}/wall": html("Just a moment...", 403),
            f"{ORIGIN}/down": fail,
            f"{ORIGIN}/file.pdf": httpx.Response(
                200, headers={"content-type": "application/pdf"}, content=b"%PDF"
            ),
        }
    )
    async with site.client() as client:
        wall = await crawl_page(client, f"{ORIGIN}/wall", 1, False, _throttle())
        down = await crawl_page(client, f"{ORIGIN}/down", 1, False, _throttle())
        pdf = await crawl_page(client, f"{ORIGIN}/file.pdf", 1, False, _throttle())
    assert wall is not None and wall.fetch_class == "blocked"
    assert down is not None and (down.status_code, down.fetch_class) == (0, "error")
    assert pdf is not None and (pdf.fetch_class, pdf.is_html) == ("ok", False)


async def test_crawl_page_honors_the_declared_charset() -> None:
    body = "<html><head><title>中文标题测试页面</title></head><body>你好</body></html>"
    response = httpx.Response(
        200, headers={"content-type": "text/html; charset=gbk"}, content=body.encode("gbk")
    )
    site = FakeSite({f"{ORIGIN}/": response})
    async with site.client() as client:
        crawled = await crawl_page(client, f"{ORIGIN}/", 0, False, _throttle())
    assert crawled is not None and crawled.title == "中文标题测试页面"


async def test_resolve_start_url_follows_redirects() -> None:
    site = FakeSite(
        {
            "https://example.com/": redirect("https://www.example.com/"),
            "https://www.example.com/": redirect("/en/"),
            "https://www.example.com/en/": html("ok"),
        }
    )
    async with site.client() as client:
        assert await resolve_start_url(client, "https://example.com/") == (
            "https://www.example.com/en/"
        )


async def test_resolve_start_url_refuses_a_hop_into_a_private_host() -> None:
    site = FakeSite({"https://example.com/": redirect("http://localhost:8080/admin")})
    async with site.client() as client:
        with pytest.raises(TargetBlocked):
            await resolve_start_url(client, "https://example.com/")
