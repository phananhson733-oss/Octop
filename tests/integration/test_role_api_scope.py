"""A role's ``api_scope`` confines its accounts to a route allowlist on the server."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from octop.api.middleware import api_scope
from tests.support.auth import bearer, login

_PASSWORD = "TestPass12"


async def _workbench_account(c: Any, auth: dict[str, str]) -> dict[str, str]:
    role = await c.post(
        "/api/users/roles",
        headers=auth,
        json={
            "user_role_name": "客户·选剧台",
            "permissions": [],
            "policies": [
                {"name": "visible_nav", "value": "workbench"},
                {"name": "allowed_plugin_ui", "value": '["sab-tools"]'},
                {"name": "allowed_expert_templates", "value": "[]"},
                {"name": "api_scope", "value": "workbench"},
            ],
        },
    )
    assert role.status_code == 201, role.text
    user = await c.post(
        "/api/users",
        headers=auth,
        json={"username": "cust", "password": _PASSWORD, "role": role.json()["user_role_id"]},
    )
    assert user.status_code == 201, user.text
    return bearer(await login(c, username="cust", password=_PASSWORD))


async def test_scoped_account_reaches_only_the_workbench_routes(env):
    c, _srv, auth = env
    cust = await _workbench_account(c, auth)

    for path in (
        "/api/auth/me",
        "/api/preferences",
        "/api/settings/capabilities",
        "/api/plugins",
        "/api/settings/timezone",
        "/api/i18n/tools",
    ):
        ok = await c.get(path, headers=cust)
        assert ok.status_code == 200, (path, ok.text)

    # Routes any signed-in account used to reach: secrets, templates, shared data, host access.
    for method, path, body in (
        ("GET", "/api/providers", None),
        ("GET", "/api/voice/providers", None),
        ("GET", "/api/experts/published", None),
        ("GET", "/api/knowledge-bases", None),
        ("GET", "/api/agents", None),
        ("GET", "/api/update/status", None),
        ("GET", "/api/filesystem/dirs", None),
        (
            "POST",
            "/api/connectors/custom-mcp/test",
            {"server": {"transport": "stdio", "command": "true"}},
        ),
        ("POST", "/api/agents", {"name": "x"}),
    ):
        denied = await c.request(method, path, headers=cust, json=body)
        assert denied.status_code == 403, (path, denied.text)

    # Own portrait: upload, show, remove. Someone else's stays refused by the route.
    me = (await c.get("/api/auth/me", headers=cust)).json()
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
        b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01"
        b"\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    up = await c.post(
        "/api/auth/me/avatar", headers=cust, files={"file": ("a.png", png, "image/png")}
    )
    assert up.status_code == 201, up.text
    assert (await c.get(f"/api/users/{me['id']}/avatar", headers=cust)).status_code == 200
    assert (await c.get("/api/users/1/avatar", headers=cust)).status_code == 403
    assert (await c.delete("/api/auth/me/avatar", headers=cust)).status_code == 204

    # Admins and roles without the policy are untouched.
    assert (await c.get("/api/providers", headers=auth)).status_code == 200


async def test_role_saves_keep_api_scope_and_reject_unknown_values(env):
    c, _srv, auth = env
    await _workbench_account(c, auth)
    roles = (await c.get("/api/users/roles", headers=auth)).json()
    role_id = next(r["user_role_id"] for r in roles if r["user_role_name"] == "客户·选剧台")

    # The dashboard form does not know the policy and leaves it out.
    saved = await c.patch(
        f"/api/users/roles/{role_id}",
        headers=auth,
        json={"policies": [{"name": "visible_nav", "value": "workbench"}]},
    )
    assert saved.status_code == 200, saved.text
    assert {"name": "api_scope", "value": "workbench"} in saved.json()["policies"]

    bad = await c.patch(
        f"/api/users/roles/{role_id}",
        headers=auth,
        json={"policies": [{"name": "api_scope", "value": "everything"}]},
    )
    assert bad.status_code == 400, bad.text


def test_an_unreadable_scope_only_signs_out():
    assert api_scope.scope_allows("none", "POST", "/api/auth/logout")
    assert not api_scope.scope_allows("none", "GET", "/api/auth/me")
    assert api_scope.scope_allows("workbench", "HEAD", "/api/plugins/sab-tools/ui/data/sab.json")
    assert not api_scope.scope_allows("workbench", "GET", "/api/plugins/sab-tools/../x")


_SCOPED_WS = (
    b"token=good",
    b"access_token=good",
    # A bad first token must not wave the good one through: routes may read the last one.
    b"token=bad&token=good",
    b"token=good&access_token=bad",
)


@pytest.mark.parametrize("query", _SCOPED_WS)
async def test_websockets_of_scoped_accounts_are_closed(monkeypatch, query):
    guard, reached, sent = _guard(monkeypatch, scoped={"good"})
    ws = {"type": "websocket", "path": "/api/browser-stream/ws", "query_string": query}
    await guard(ws, None, _sink(sent))
    assert reached == [] and sent[0] == {
        "type": "websocket.close",
        "code": 4003,
        "reason": "forbidden",
    }


async def test_a_bad_bearer_header_does_not_hide_a_scoped_query_token(monkeypatch):
    guard, reached, sent = _guard(monkeypatch, scoped={"good"})
    ws = {
        "type": "websocket",
        "path": "/api/stt-stream",
        "query_string": b"token=good",
        "headers": [(b"authorization", b"Bearer bad")],
    }
    await guard(ws, None, _sink(sent))
    assert reached == [] and sent[0]["code"] == 4003
    header_only = {**ws, "query_string": b"", "headers": [(b"authorization", b"Bearer good")]}
    sent.clear()
    await guard(header_only, None, _sink(sent))
    assert reached == [] and sent[0]["code"] == 4003


async def test_websockets_of_other_accounts_reach_the_route(monkeypatch):
    guard, reached, sent = _guard(monkeypatch, scoped=set())
    for query in (b"token=good", b"token=bad", b""):
        ws = {"type": "websocket", "path": "/api/notifications/ws", "query_string": query}
        await guard(ws, None, _sink(sent))
    assert reached == ["/api/notifications/ws"] * 3 and sent == []


def _guard(monkeypatch: Any, *, scoped: set[str]) -> tuple[Any, list[str], list[dict[str, Any]]]:
    from octop.infra.errors import ErrorCode, OctopError

    async def resolve(_server: Any, token: str) -> Any:
        if token == "bad":
            raise OctopError(ErrorCode.AUTH_FAILED, "invalid token")
        return SimpleNamespace(is_admin=False, role="r", token=token)

    monkeypatch.setattr(api_scope, "resolve_user_from_token_async", resolve)
    monkeypatch.setattr(
        api_scope, "scope_of", lambda _server, user: "workbench" if user.token in scoped else None
    )
    reached: list[str] = []

    async def app(asgi_scope: dict[str, Any], _receive: Any, _send: Any) -> None:
        reached.append(asgi_scope["path"])

    return api_scope.WebSocketScopeGuard(app, server=object()), reached, []


def _sink(sent: list[dict[str, Any]]) -> Any:
    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    return send
