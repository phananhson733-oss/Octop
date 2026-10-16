"""Render sampled pages in headless Chromium and compare them with the served HTML.

The static crawl reads what the server sends. Search engines also run the page's
JavaScript, and what the script does to the page can undo it: a valid product page
that writes ``noindex`` and a "not found" title while its data loads, an H1 replaced
after hydration, links that exist only in the rendered DOM. An init script records
every title and robots state the page passes through, not just the final one.

Chromium resolves hosts itself, outside ``net.GuardedBackend``. Every request and
every WebSocket a page opens is routed through a guard that resolves the host and
refuses it unless all addresses are public, before Chromium connects. WebRTC may not
send UDP outside a proxy, and there is none. What remains unchecked: a DNS answer that
changes between the guard's lookup and Chromium's (rebinding), WebSockets opened inside
web workers, and WebRTC relays over TCP. Audits are admin-only and limited to the
project's own site, and service workers are blocked.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable, Collection
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from octop.infra.seo.audit.net import (
    SITE_HEADERS_ENV,
    USER_AGENT,
    Resolver,
    resolve_host,
    site_headers,
)
from octop.infra.seo.audit.urls import (
    is_blocked_host,
    is_public_ip,
    is_same_origin,
    normalize_host,
    normalize_url,
)

logger = logging.getLogger(__name__)

CHROMIUM_ENV = "OCTOP_SEO_AUDIT_CHROMIUM"
GOTO_TIMEOUT_MS = 20_000
NETWORK_IDLE_MS = 5_000
# Data fetched after the load event often lands a moment later.
SETTLE_MS = 1_500
MAX_REQUESTS_PER_PAGE = 300
MAX_TRACE_STATES = 200
MAX_RENDERED_ONLY = 100
_SKIPPED_RESOURCES = frozenset({"image", "media", "font"})
_VIEWPORT = {"width": 412, "height": 915}
# One headless browser at a time in this process, for audits and inspections alike.
_BROWSER_SLOT = asyncio.Lock()

# Records every (title, robots) state from the first parsed byte on.
_TRACE_SCRIPT = f"""
(() => {{
  const trace = [];
  const start = performance.now();
  const robots = () => Array.from(
    document.querySelectorAll('meta[name="robots" i], meta[name="googlebot" i]'),
    (meta) => meta.getAttribute('content') || ''
  ).join(', ');
  const snap = () => {{
    const state = {{ t: Math.round(performance.now() - start), title: document.title, robots: robots() }};
    const last = trace[trace.length - 1];
    if (trace.length < {MAX_TRACE_STATES} && (!last || last.title !== state.title || last.robots !== state.robots)) {{
      trace.push(state);
    }}
  }};
  Object.defineProperty(window, '__octopSeoTrace', {{ value: trace }});
  new MutationObserver(snap).observe(document, {{
    subtree: true, childList: true, characterData: true,
    attributes: true, attributeFilter: ['content', 'name'],
  }});
  document.addEventListener('DOMContentLoaded', snap);
  window.addEventListener('load', snap);
}})();
"""

# ``details`` adds the heading outline and the main region's visible text (inspection).
_EXTRACT_SCRIPT = """
(details) => {
  const text = document.body ? document.body.innerText : '';
  const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const main = document.querySelector('main') || document.querySelector('[role="main"]')
    || document.querySelector('article') || document.body;
  const canonical = document.querySelector('link[rel~="canonical" i][href]');
  return {
    title: document.title,
    robots: Array.from(
      document.querySelectorAll('meta[name="robots" i], meta[name="googlebot" i]'),
      (meta) => meta.getAttribute('content') || ''
    ).join(', '),
    canonical: canonical ? canonical.href : null,
    // textContent, as the HTML parser sees it; innerText would apply CSS text-transform.
    h1s: Array.from(document.querySelectorAll('h1'), (h) => (h.textContent || '').replace(/\\s+/g, ' ').trim())
      .filter(Boolean).slice(0, 5).map((h) => h.slice(0, 200)),
    word_count: text.split(/\\s+/).filter(Boolean).length,
    links: Array.from(document.querySelectorAll('a[href]'), (a) => a.href)
      .filter((href) => /^https?:/i.test(href)).slice(0, 1000),
    unlinked: document.querySelectorAll('a:not([href]), [role="link"]:not([href])').length,
    json_ld_blocks: document.querySelectorAll('script[type="application/ld+json" i]').length,
    trace: window.__octopSeoTrace || [],
    // Where the shown document was loaded from: a script redirect changes it, while
    // history.replaceState (which only rewrites the address bar) does not.
    document_url: (performance.getEntriesByType('navigation')[0] || {}).name || location.href,
    outline: details
      ? Array.from(document.querySelectorAll('h1, h2, h3, h4, h5, h6'),
          (h) => [Number(h.tagName[1]), clean(h.textContent).slice(0, 150)])
          .filter((entry) => entry[1]).slice(0, 60)
      : [],
    main_text: details && main ? main.innerText.slice(0, 40000) : '',
  };
}
"""


@dataclass
class Rendered:
    """What the page looked like after its scripts ran, and on the way there."""

    status: int | None = None
    final_url: str | None = None
    title: str = ""
    robots: str = ""
    canonical: str | None = None
    h1s: list[str] = field(default_factory=list)
    word_count: int = 0
    links: list[str] = field(default_factory=list)
    unlinked: int = 0
    json_ld_blocks: int = 0
    # [{"t": ms since navigation, "title": ..., "robots": ...}] for every change.
    trace: list[dict[str, Any]] = field(default_factory=list)
    load_timed_out: bool = False
    error: str | None = None
    # Only with ``details``: [level, text] per heading, and the main region's visible
    # text (innerText: collapsed accordions and other hidden text are left out).
    outline: list[list[Any]] = field(default_factory=list)
    main_text: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class Renderer(Protocol):
    async def render(self, url: str, *, details: bool = False) -> Rendered: ...


class RenderUnavailable(Exception):
    """No browser could be started; the audit records why and goes on without rendering."""


RendererFactory = Callable[[str], AbstractAsyncContextManager[Renderer]]


class _HostGuard:
    def __init__(self, resolver: Resolver) -> None:
        self._resolve = resolver
        self._verdicts: dict[str, bool] = {}

    async def allows(self, host: str) -> bool:
        name = normalize_host(host)
        if name not in self._verdicts:
            self._verdicts[name] = await self._check(name)
        return self._verdicts[name]

    async def _check(self, name: str) -> bool:
        if not name or is_blocked_host(name):
            return False
        # A public IP literal; a private one was refused above.
        if is_public_ip(name):
            return True
        try:
            addresses = await self._resolve(name, 443)
        except OSError:
            return False
        return bool(addresses) and all(is_public_ip(address) for address in addresses)


class ChromiumRenderer:
    """One headless browser for one audit's render phase."""

    def __init__(self, browser: Any, site_host: str, resolver: Resolver) -> None:
        self._browser = browser
        self._site = normalize_host(site_host).removeprefix("www.")
        self._headers = site_headers(os.environ.get(SITE_HEADERS_ENV)).get(self._site, {})
        self._guard = _HostGuard(resolver)

    def _own_site(self, host: str) -> bool:
        return normalize_host(host).removeprefix("www.") == self._site

    async def render(self, url: str, *, details: bool = False) -> Rendered:
        try:
            context = await self._browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
                    f"Chrome/{self._browser.version} Mobile Safari/537.36 {USER_AGENT}"
                ),
                viewport=_VIEWPORT,
                is_mobile=True,
                has_touch=True,
                service_workers="block",
                accept_downloads=False,
            )
        except Exception as exc:  # noqa: BLE001 - a crashed browser fails the page, not the audit
            return Rendered(error=_short(exc))
        result = Rendered()
        requests = 0
        capped = False

        async def guard(route: Any) -> None:
            nonlocal requests, capped
            request = route.request
            parts = urlsplit(request.url)
            if (
                request.resource_type in _SKIPPED_RESOURCES
                or parts.scheme not in {"http", "https"}
                or not await self._guard.allows(parts.hostname or "")
            ):
                await route.abort()
                return
            # Only requests the page really makes count toward the cap.
            requests += 1
            if requests > MAX_REQUESTS_PER_PAGE:
                capped = True
                await route.abort()
                return
            # The site's secret audit header goes to the site alone, never to third parties.
            if self._headers and self._own_site(parts.hostname or ""):
                await route.continue_(headers={**request.headers, **self._headers})
            else:
                await route.continue_()

        async def guard_socket(socket: Any) -> None:
            parts = urlsplit(socket.url)
            if parts.scheme in {"ws", "wss"} and await self._guard.allows(parts.hostname or ""):
                socket.connect_to_server()
            else:
                await socket.close()

        try:
            await context.route("**/*", guard)
            # context.route does not see WebSockets.
            await context.route_web_socket(lambda _url: True, guard_socket)
            await context.add_init_script(_TRACE_SCRIPT)
            page = await context.new_page()
            try:
                response = await page.goto(url, wait_until="load", timeout=GOTO_TIMEOUT_MS)
                result.status = response.status if response is not None else None
            except Exception as exc:  # noqa: BLE001 - a slow page is still worth reading
                if "Timeout" not in type(exc).__name__:
                    result.error = _short(exc)
                    return result
                result.load_timed_out = True
            # Pages that keep polling never go idle.
            with suppress(Exception):
                await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_MS)
            await page.wait_for_timeout(SETTLE_MS)
            data = await page.evaluate(_EXTRACT_SCRIPT, details)
            result.final_url = data.get("document_url") or page.url
            for key in (
                "title",
                "robots",
                "canonical",
                "h1s",
                "word_count",
                "links",
                "unlinked",
                "json_ld_blocks",
                "trace",
                "outline",
                "main_text",
            ):
                setattr(result, key, data.get(key, getattr(result, key)))
            if capped:
                # Requests past the cap were refused, so the page may show a state it
                # only reached because its own data never arrived: nothing to compare.
                result.error = (
                    f"stopped after {MAX_REQUESTS_PER_PAGE} requests; the page did not "
                    "finish loading"
                )
            elif result.load_timed_out:
                # The same goes for a page whose load event never came: its data may still
                # be on the way.
                result.error = (
                    f"the page did not finish loading within {GOTO_TIMEOUT_MS // 1000} seconds"
                )
        except Exception as exc:  # noqa: BLE001 - one page must not stop the render phase
            result.error = _short(exc)
        finally:
            # After a browser crash closing fails too; the page is already marked failed.
            with suppress(Exception):
                await context.close()
        return result


def render_record(
    rendered: Rendered, page_url: str, html_links: Collection[str] | None
) -> dict[str, Any]:
    """A render as the comparison rules read it: canonical normalized, links reduced.

    ``html_links`` are the internal links of the page's HTML, or None when that list is
    incomplete (a cut-off body); then no link is called rendered-only.
    """
    record = rendered.to_json()
    links = record.pop("links")
    if rendered.canonical:
        record["canonical"] = normalize_url(rendered.canonical) or rendered.canonical
    final_url = normalize_url(rendered.final_url) if rendered.final_url else None
    if final_url is not None:
        record["final_url"] = final_url
    # A page that failed, or that sent the browser elsewhere, has no links of its own to diff.
    if rendered.error is not None or final_url not in (None, page_url):
        return record
    internal = list(
        dict.fromkeys(
            target
            for target in (normalize_url(link) for link in links)
            if target and is_same_origin(target, page_url)
        )
    )
    record["internal_links"] = len(internal)
    record["rendered_only"] = None
    if html_links is not None:
        only = [link for link in internal if link not in html_links and link != page_url]
        record["rendered_only_count"] = len(only)
        record["rendered_only"] = only[:MAX_RENDERED_ONLY]
        record["link_status"] = {}
    return record


def _short(exc: BaseException) -> str:
    return (str(exc).splitlines() or [type(exc).__name__])[0][:300]


@asynccontextmanager
async def open_chromium_renderer(
    start_url: str, resolver: Resolver = resolve_host, *, wait_s: float | None = None
) -> AsyncIterator[Renderer]:
    """A Chromium renderer for one audit; ``OCTOP_SEO_AUDIT_CHROMIUM`` names the binary.

    The process runs one browser at a time. A caller passing ``wait_s`` gives up after
    that long while another holds it; without it, the caller waits its turn.
    """
    try:
        async with asyncio.timeout(wait_s):
            await _BROWSER_SLOT.acquire()
    except TimeoutError as exc:
        raise RenderUnavailable("the browser is busy with another render; try again later") from exc
    try:
        async with _started_renderer(start_url, resolver) as renderer:
            yield renderer
    finally:
        _BROWSER_SLOT.release()


@asynccontextmanager
async def _started_renderer(start_url: str, resolver: Resolver) -> AsyncIterator[Renderer]:
    try:
        from playwright.async_api import async_playwright  # noqa: PLC0415
    except ImportError as exc:
        raise RenderUnavailable("playwright is not installed") from exc
    executable = os.environ.get(CHROMIUM_ENV) or None
    try:
        playwright = await async_playwright().start()
    except Exception as exc:
        logger.warning("SEO audit could not start Playwright: %s", exc)
        raise RenderUnavailable(_short(exc)) from exc
    try:
        try:
            # Playwright passes --no-sandbox itself, so this also starts as root in a container.
            browser = await playwright.chromium.launch(
                headless=True,
                executable_path=executable,
                args=[
                    "--disable-dev-shm-usage",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                ],
            )
        except Exception as exc:
            logger.warning("SEO audit could not start Chromium: %s", exc)
            raise RenderUnavailable(_short(exc)) from exc
        try:
            yield ChromiumRenderer(browser, urlsplit(start_url).hostname or "", resolver)
        finally:
            with suppress(Exception):
                await browser.close()
    finally:
        with suppress(Exception):
            await playwright.stop()
