"""A fake website served through ``httpx.MockTransport`` for crawler and runner tests."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from octop.infra.seo.audit.net import crawler_client

Route = Callable[[httpx.Request], httpx.Response]


def html(body: str, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(
        status, headers={"content-type": "text/html; charset=utf-8", **headers}, text=body
    )


def redirect(location: str, status: int = 301) -> httpx.Response:
    return httpx.Response(status, headers={"location": location})


def page(title: str, *links: str, words: int = 200) -> httpx.Response:
    anchors = "".join(f'<a href="{link}">{link}</a>' for link in links)
    text = " ".join(["word"] * words)
    description = f"{title} " + "described in enough words to pass the short check " * 2
    return html(
        f"<html><head><title>{title} page title</title>"
        f'<meta name="description" content="{description}"></head>'
        f"<body><h1>{title}</h1><p>{text}</p>{anchors}</body></html>"
    )


class FakeSite:
    def __init__(self, routes: dict[str, Route | httpx.Response]) -> None:
        self.routes = routes
        self.requests: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(f"{request.method} {url}")
        route = self.routes.get(url)
        if route is None:
            return httpx.Response(404, headers={"content-type": "text/html"}, text="missing")
        return route(request) if callable(route) else route

    def client(self) -> httpx.AsyncClient:
        return crawler_client(httpx.MockTransport(self.handle))
