"""Desktop and mobile streams read the token from the start message, past the handshake guard."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from octop.api.routers.desktop import stream as desktop_stream
from octop.api.routers.mobile import stream as mobile_stream


class _Socket:
    def __init__(self) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(octop_server=SimpleNamespace()))
        self.headers: dict[str, str] = {}
        self.query_params: dict[str, str] = {}
        self.closed: tuple[int, str] | None = None
        self.sent: list[str] = []

    async def accept(self) -> None:
        return None

    async def receive_text(self) -> str:
        return json.dumps({"type": "start", "token": "tok"})

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)

    @property
    def application_state(self) -> Any:
        from starlette.websockets import WebSocketState

        return WebSocketState.DISCONNECTED if self.closed else WebSocketState.CONNECTED


_READY = SimpleNamespace(setup_state="ready", ok=True, reason="", selected_device="")


@pytest.mark.parametrize(
    ("module", "handler", "status_name"),
    [
        (desktop_stream, "desktop_stream_ws", "desktop_status"),
        (mobile_stream, "mobile_stream_ws", "mobile_status"),
    ],
)
async def test_scoped_accounts_are_closed_after_the_start_message(
    monkeypatch, module, handler, status_name
):
    monkeypatch.setattr(module, status_name, lambda *_a, **_k: _READY)
    monkeypatch.setattr(module, "resolve_request_locale", lambda _ws: "en", raising=False)
    monkeypatch.setattr(
        module, "resolve_user_from_token", lambda _s, _t: SimpleNamespace(id=7, is_admin=False)
    )
    monkeypatch.setattr(module, "user_has_permission", lambda _u, _k: True)
    monkeypatch.setattr(module, "scope_of", lambda _s, _u: "workbench")
    ws = _Socket()
    ws.app.state.octop_server.services = SimpleNamespace(config=SimpleNamespace())

    await getattr(module, handler)(ws, token=None)

    assert ws.closed == (4003, "permission required")
