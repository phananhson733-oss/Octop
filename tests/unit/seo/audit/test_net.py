"""The crawler connects only to public addresses, whatever a hostname resolves to."""

import json
import logging
from typing import Any

import httpcore
import httpx
import pytest

from octop.infra.seo.audit.net import (
    SITE_HEADERS_ENV,
    USER_AGENT,
    GuardedBackend,
    crawler_client,
)
from octop.infra.seo.audit.urls import TargetBlocked, is_public_ip, validate_start_url


class _Inner(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.connected: list[str] = []

    async def connect_tcp(self, host: str, port: int, **_: Any) -> httpcore.AsyncNetworkStream:
        self.connected.append(host)
        raise httpcore.ConnectError("test stops here")


def _backend(answers: list[str]) -> tuple[GuardedBackend, _Inner]:
    async def resolve(_host: str, _port: int) -> list[str]:
        return answers

    inner = _Inner()
    return GuardedBackend(resolve, inner), inner


@pytest.mark.parametrize(
    "answers",
    [
        ["127.0.0.1"],
        ["10.0.0.5"],
        ["169.254.169.254"],
        ["::1"],
        ["::ffff:127.0.0.1"],
        ["fd00::1"],
        # One private answer is enough: the resolver may pick it at connect time.
        ["93.184.216.34", "192.168.1.1"],
        [],
    ],
)
async def test_private_answers_are_refused(answers: list[str]) -> None:
    backend, inner = _backend(answers)
    with pytest.raises(TargetBlocked):
        await backend.connect_tcp("rebind.example", 443)
    assert inner.connected == []


async def test_public_answers_connect_by_address() -> None:
    backend, inner = _backend(["93.184.216.34"])
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("example.com", 443)
    assert inner.connected == ["93.184.216.34"]


@pytest.mark.parametrize("host", ["localhost", "printer.local", "metadata.google.internal"])
async def test_blocked_names_are_refused_before_resolving(host: str) -> None:
    backend, inner = _backend(["93.184.216.34"])
    with pytest.raises(TargetBlocked):
        await backend.connect_tcp(host, 80)
    assert inner.connected == []


async def test_ip_literals_and_unix_sockets() -> None:
    backend, inner = _backend([])
    with pytest.raises(TargetBlocked):
        await backend.connect_tcp("127.0.0.1", 80)
    with pytest.raises(TargetBlocked):
        await backend.connect_unix_socket("/var/run/docker.sock")
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("8.8.8.8", 80)
    assert inner.connected == ["8.8.8.8"]


def test_is_public_ip_unwraps_embedded_ipv4() -> None:
    assert is_public_ip("8.8.8.8")
    assert not is_public_ip("::ffff:10.0.0.1")
    assert not is_public_ip("2002:0a00:0001::")  # 6to4 around 10.0.0.1
    assert not is_public_ip("64:ff9b::7f00:1")  # NAT64 around 127.0.0.1
    assert not is_public_ip("not an address")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("example.com", "https://example.com/"),
        ("http://example.com/shop", "http://example.com/shop"),
    ],
)
def test_validate_start_url(value: str, expected: str) -> None:
    assert validate_start_url(value) == expected


@pytest.mark.parametrize("value", ["ftp://example.com", "https://user:pw@example.com", ""])
def test_validate_start_url_rejects(value: str) -> None:
    with pytest.raises(ValueError):
        validate_start_url(value)


def test_validate_start_url_blocks_private_hosts() -> None:
    with pytest.raises(TargetBlocked):
        validate_start_url("http://localhost:8000")


async def test_client_sends_its_user_agent_and_never_follows_redirects() -> None:
    async with crawler_client() as client:
        assert client.headers["User-Agent"] == USER_AGENT
        assert client.follow_redirects is False
        assert client.trust_env is False


def _header_seen(seen: dict[str, str | None]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.headers.get("x-audit-key")
        return httpx.Response(200)

    return httpx.MockTransport(handle)


async def test_site_headers_go_only_to_their_own_site() -> None:
    seen: dict[str, str | None] = {}
    config = json.dumps({"Example.com": {"x-audit-key": "s3cret"}})
    async with crawler_client(_header_seen(seen), headers_json=config) as client:
        for url in (
            "https://example.com/",
            "https://www.example.com/a",
            "https://sub.example.com/",
            "https://other.example/",
        ):
            await client.get(url)
    assert seen == {
        "example.com": "s3cret",
        "www.example.com": "s3cret",
        "sub.example.com": None,
        "other.example": None,
    }


async def test_site_headers_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str | None] = {}
    monkeypatch.setenv(SITE_HEADERS_ENV, json.dumps({"www.example.com": {"x-audit-key": "k"}}))
    async with crawler_client(_header_seen(seen)) as client:
        await client.get("https://example.com/")
    assert seen == {"example.com": "k"}


@pytest.mark.parametrize("config", ["not json", '["example.com"]', '{"example.com": "k"}'])
async def test_malformed_site_headers_are_ignored_without_logging_them(
    config: str, caplog: pytest.LogCaptureFixture
) -> None:
    seen: dict[str, str | None] = {}
    with caplog.at_level(logging.WARNING):
        async with crawler_client(_header_seen(seen), headers_json=config) as client:
            await client.get("https://example.com/")
    assert seen == {"example.com": None}
    assert SITE_HEADERS_ENV in caplog.text and config not in caplog.text
