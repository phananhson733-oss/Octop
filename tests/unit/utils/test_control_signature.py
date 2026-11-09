"""Unified-entry control signatures bind key, audience, request and time."""

from __future__ import annotations

import pytest

from octop.infra.utils.control_signature import (
    MAX_SKEW_SECONDS,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    control_headers,
    verify_control_signature,
)

KEY = "k" * 32
NOW = 1_800_000_000.0
REQUEST = {
    "method": "PUT",
    "path": "/api/control/users/7/grants",
    "query": "",
    "body": b'{"role":"user"}',
    "audience": "internal",
}


def _signed(**overrides: object) -> dict[str, str]:
    request = {**REQUEST, **overrides}
    return control_headers(
        KEY,
        method=str(request["method"]),
        path=str(request["path"]),
        body=request["body"],  # type: ignore[arg-type]
        audience=str(request["audience"]),
        query=str(request["query"]),
        now=NOW,
    )


def _verify(headers: dict[str, str | None], key: str = KEY, now: float = NOW, **request) -> bool:
    return verify_control_signature(
        key,
        **{**REQUEST, **request},
        timestamp=headers.get(TIMESTAMP_HEADER),
        signature=headers.get(SIGNATURE_HEADER),
        now=now,
    )


def test_valid_signature_is_accepted_inside_the_window():
    headers = _signed()
    assert _verify(headers)
    assert _verify(headers, now=NOW + MAX_SKEW_SECONDS)
    assert _verify(headers, now=NOW - MAX_SKEW_SECONDS)


@pytest.mark.parametrize(
    "change",
    [
        {"key": "x" * 32},
        {"audience": "customer"},
        {"method": "POST"},
        {"path": "/api/control/users/8/grants"},
        {"query": "a=1"},
        {"body": b'{"role":"admin"}'},
        {"now": NOW + MAX_SKEW_SECONDS + 1},
        {"now": NOW - MAX_SKEW_SECONDS - 1},
    ],
)
def test_any_other_key_target_request_or_time_is_rejected(change):
    assert not _verify(_signed(), **change)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {TIMESTAMP_HEADER: str(int(NOW))},
        {SIGNATURE_HEADER: "0" * 64},
        {TIMESTAMP_HEADER: "-1", SIGNATURE_HEADER: "0" * 64},
        {TIMESTAMP_HEADER: "soon", SIGNATURE_HEADER: "0" * 64},
    ],
)
def test_missing_or_malformed_headers_are_rejected(headers):
    assert not _verify(headers)


def test_a_signature_reused_with_a_fresh_timestamp_is_rejected():
    headers = _signed()
    headers[TIMESTAMP_HEADER] = str(int(NOW) + 1)
    assert not _verify(headers)
