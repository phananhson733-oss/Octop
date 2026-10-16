"""Crawler HTTP client that refuses private targets at connect time.

Every connection resolves the host itself and requires every returned address
to be public before connecting to one of them, so DNS rebinding, a redirect
into a private network, or a numeric host alias cannot reach internal services.
TLS still verifies against the hostname: httpcore sends SNI from the URL, not
from the address it connected to. Redirects are never followed by the client.

``OCTOP_SEO_AUDIT_HEADERS`` adds request headers for named sites, so a site's own
firewall can let the crawler through by a secret header instead of its public
User-Agent.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import ssl
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import anyio
import httpcore
import httpx

from octop.infra.seo.audit.urls import TargetBlocked, is_blocked_host, is_public_ip, normalize_host

logger = logging.getLogger(__name__)

USER_AGENT = "Octop-SEO-Audit/1.0"
SITE_HEADERS_ENV = "OCTOP_SEO_AUDIT_HEADERS"
Resolver = Callable[[str, int], Awaitable[list[str]]]


async def resolve_host(host: str, port: int) -> list[str]:
    infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _is_ip(host: str) -> bool:
    try:
        socket.inet_pton(socket.AF_INET6 if ":" in host else socket.AF_INET, host)
    except OSError:
        return False
    return True


class GuardedBackend(httpcore.AsyncNetworkBackend):
    def __init__(
        self,
        resolver: Resolver = resolve_host,
        inner: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._resolve = resolver
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        name = normalize_host(host)
        if is_blocked_host(name):
            raise TargetBlocked(name)
        if _is_ip(name):
            addresses = [name]
        else:
            try:
                with anyio.fail_after(timeout):
                    addresses = await self._resolve(name, port)
            except (OSError, TimeoutError) as exc:
                raise httpcore.ConnectError(f"Could not resolve {name}") from exc
        # A host is refused if any address is private, not only the one we would pick.
        if not addresses or not all(is_public_ip(address) for address in addresses):
            raise TargetBlocked(name)
        error: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                error = exc
        assert error is not None
        raise error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise TargetBlocked("unix socket")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """``AsyncHTTPTransport`` without proxies or env settings, over ``GuardedBackend``."""

    def __init__(self, backend: httpcore.AsyncNetworkBackend | None = None) -> None:
        super().__init__(trust_env=False)
        context: ssl.SSLContext = httpx.create_ssl_context(trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=context,
            max_connections=10,
            network_backend=backend or GuardedBackend(),
        )


def _site(host: str) -> str:
    return normalize_host(host).removeprefix("www.")


def site_headers(raw: str | None) -> dict[str, dict[str, str]]:
    """Headers per site from JSON such as ``{"example.com": {"x-audit-key": "..."}}``.

    A site's headers go only to that host and its www. form, never to other hosts,
    redirect targets included. Malformed JSON is logged (without its values) and ignored.
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return {
            _site(host): {str(name): str(value) for name, value in headers.items()}
            for host, headers in data.items()
        }
    except (ValueError, AttributeError, TypeError):
        logger.warning("%s is not a JSON object of site -> headers; ignoring it", SITE_HEADERS_ENV)
        return {}


def crawler_client(
    transport: httpx.AsyncBaseTransport | None = None, *, headers_json: str | None = None
) -> httpx.AsyncClient:
    """The audit client; ``headers_json`` defaults to ``OCTOP_SEO_AUDIT_HEADERS``."""
    extra = site_headers(os.environ.get(SITE_HEADERS_ENV) if headers_json is None else headers_json)

    async def add_site_headers(request: httpx.Request) -> None:
        if headers := extra.get(_site(request.url.host)):
            request.headers.update(headers)

    return httpx.AsyncClient(
        transport=transport or GuardedTransport(),
        trust_env=False,
        follow_redirects=False,
        timeout=15.0,
        headers={"User-Agent": USER_AGENT},
        event_hooks={"request": [add_site_headers]} if extra else None,
    )
