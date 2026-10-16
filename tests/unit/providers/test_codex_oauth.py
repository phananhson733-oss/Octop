"""Unit tests for Codex OAuth helpers."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from unittest import mock
from urllib.parse import urlparse

import pytest

from octop.infra.agents.providers.codex_apply import CODEX_FALLBACK_MODELS, CODEX_PROVIDER_NAME
from octop.infra.agents.providers.codex_oauth import (
    CodexOAuthDeviceCodeError,
    build_codex_headers,
    exchange_device_code,
    fetch_codex_models,
    poll_device_token,
    request_device_code,
)
from octop.infra.agents.providers.presets import load_provider_presets


class _Resp:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_request_device_code_parses_response() -> None:
    payload = json.dumps(
        {"device_auth_id": "dev-1", "user_code": "ABCD-1234", "interval": 5}
    ).encode()

    def fake_urlopen(req: urllib.request.Request, timeout: float = 30) -> _Resp:
        assert "deviceauth/usercode" in req.full_url
        return _Resp(payload)

    with mock.patch("urllib.request.urlopen", fake_urlopen):
        info = request_device_code()

    assert info["device_auth_id"] == "dev-1"
    assert info["user_code"] == "ABCD-1234"
    assert info["interval_s"] == 5
    assert urlparse(info["verification_url"]).hostname == "auth.openai.com"


def test_request_device_code_normalizes_network_failures() -> None:
    def fake_urlopen(req: urllib.request.Request, timeout: float = 30) -> _Resp:
        raise urllib.error.URLError("DNS lookup failed")

    with (
        mock.patch("urllib.request.urlopen", fake_urlopen),
        pytest.raises(CodexOAuthDeviceCodeError) as raised,
    ):
        request_device_code()

    assert raised.value.reason == "network_error"
    assert raised.value.upstream_status is None
    assert "DNS lookup failed" not in str(raised.value)


def test_request_device_code_preserves_upstream_http_status() -> None:
    def fake_urlopen(req: urllib.request.Request, timeout: float = 30) -> _Resp:
        raise urllib.error.HTTPError(req.full_url, 503, "unavailable", {}, None)  # type: ignore[arg-type]

    with (
        mock.patch("urllib.request.urlopen", fake_urlopen),
        pytest.raises(CodexOAuthDeviceCodeError) as raised,
    ):
        request_device_code()

    assert raised.value.reason == "upstream_http_error"
    assert raised.value.upstream_status == 503


def test_poll_device_token_returns_none_while_pending() -> None:
    def fake_urlopen(req: urllib.request.Request, timeout: float = 30) -> _Resp:
        raise urllib.error.HTTPError(req.full_url, 403, "pending", {}, None)  # type: ignore[arg-type]

    with mock.patch("urllib.request.urlopen", fake_urlopen):
        result = poll_device_token("dev-1", "ABCD-1234")

    assert result is None


def test_poll_device_token_returns_code_on_success() -> None:
    payload = json.dumps({"authorization_code": "auth-code", "code_verifier": "verifier"}).encode()

    def fake_urlopen(req: urllib.request.Request, timeout: float = 30) -> _Resp:
        assert "deviceauth/token" in req.full_url
        return _Resp(payload)

    with mock.patch("urllib.request.urlopen", fake_urlopen):
        result = poll_device_token("dev-1", "ABCD-1234")

    assert result == ("auth-code", "verifier")


def test_exchange_device_code_builds_credentials() -> None:
    payload = json.dumps(
        {"access_token": "access-tok", "refresh_token": "refresh-tok", "expires_in": 3600}
    ).encode()

    def fake_urlopen(req: urllib.request.Request, timeout: float = 30) -> _Resp:
        assert req.full_url.endswith("/oauth/token")
        return _Resp(payload)

    with mock.patch("urllib.request.urlopen", fake_urlopen):
        cred = exchange_device_code("auth-code", "verifier")

    assert cred["access"] == "access-tok"
    assert cred["refresh"] == "refresh-tok"


def test_build_codex_headers_includes_account_id() -> None:
    headers = build_codex_headers("acct-123")
    assert headers["originator"] == "openclaw"
    assert headers["chatgpt-account-id"] == "acct-123"
    assert "User-Agent" in headers


def test_codex_provider_constants() -> None:
    assert CODEX_PROVIDER_NAME == "openai-codex"
    ids = [m["id"] for m in CODEX_FALLBACK_MODELS]
    assert ids[0] == "gpt-6-astra"
    # Rejected by the ChatGPT backend for ChatGPT accounts since 2026-10.
    assert not {"gpt-5.4", "gpt-5.4-mini", "gpt-5.5"} & set(ids)


def test_codex_preset_uses_fallback_models() -> None:
    preset = next(p for p in load_provider_presets() if p["id"] == "openai-codex")
    assert [m["id"] for m in preset["models"]] == [m["id"] for m in CODEX_FALLBACK_MODELS]


def _catalog_entry(slug: str, *, priority: int, visibility: str = "list") -> dict[str, object]:
    return {
        "slug": slug,
        "display_name": slug.upper(),
        "visibility": visibility,
        "priority": priority,
        "input_modalities": ["text", "image"],
    }


def test_fetch_codex_models_keeps_listed_models_by_priority() -> None:
    payload = json.dumps(
        {
            "models": [
                _catalog_entry("gpt-6-sol", priority=3),
                _catalog_entry("gpt-reserve", priority=4, visibility="hide"),
                _catalog_entry("gpt-6.1-sol", priority=1),
                _catalog_entry("gpt-6-astra", priority=2),
            ]
        }
    ).encode()
    seen: dict[str, object] = {}

    def fake_urlopen(req: urllib.request.Request, timeout: float = 30) -> _Resp:
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _Resp(payload)

    with mock.patch("urllib.request.urlopen", fake_urlopen):
        models = fetch_codex_models("access-tok", "acct-123")

    url = urlparse(str(seen["url"]))
    assert url.hostname == "chatgpt.com"
    assert url.path == "/backend-api/codex/models"
    assert "client_version=" in url.query
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == "Bearer access-tok"
    assert headers["chatgpt-account-id"] == "acct-123"
    assert models == [
        {"id": "gpt-6.1-sol", "name": "GPT-6.1-SOL", "enabled": True, "input": ["text", "image"]},
        {"id": "gpt-6-astra", "name": "GPT-6-ASTRA", "enabled": True, "input": ["text", "image"]},
        {"id": "gpt-6-sol", "name": "GPT-6-SOL", "enabled": True, "input": ["text", "image"]},
    ]


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.HTTPError("https://chatgpt.com", 401, "unauthorized", {}, None),  # type: ignore[arg-type]
        urllib.error.URLError("DNS lookup failed"),
        http.client.IncompleteRead(b"{"),
        b"not json",
        json.dumps({"detail": "nope"}).encode(),
        json.dumps(
            {"models": [_catalog_entry("gpt-reserve", priority=4, visibility="hide")]}
        ).encode(),
    ],
)
def test_fetch_codex_models_returns_none_when_catalog_unusable(failure: object) -> None:
    def fake_urlopen(req: urllib.request.Request, timeout: float = 30) -> _Resp:
        if isinstance(failure, Exception):
            raise failure
        assert isinstance(failure, bytes)
        return _Resp(failure)

    with mock.patch("urllib.request.urlopen", fake_urlopen):
        assert fetch_codex_models("access-tok", "acct-123") is None
