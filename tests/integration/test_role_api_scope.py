"""A role's ``api_scope`` confines its accounts to a route allowlist on the server."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from octop.api.middleware import api_scope
from tests.support.auth import bearer, login

_PASSWORD = "TestPass12"


async def _workbench_account(
    c: Any, auth: dict[str, str], *, plugins: tuple[str, ...] = ("sab-tools",)
) -> dict[str, str]:
    role = await c.post(
        "/api/users/roles",
        headers=auth,
        json={
            "user_role_name": "客户·选剧台",
            "permissions": [],
            "policies": [
                {"name": "visible_nav", "value": "workbench"},
                {"name": "allowed_plugin_ui", "value": json.dumps(list(plugins))},
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


async def test_the_checked_path_is_the_routed_path(env):
    c, _srv, auth = env
    cust = await _workbench_account(c, auth)
    # An encoded "?" must not cut the checked path down to an allowed route.
    sneaky = await c.get("/api/settings/capabilities%3F/x", headers=cust)
    assert sneaky.status_code == 403, sneaky.text


async def test_scoped_accounts_get_plugin_page_files_only(env):
    from octop.api.routers.plugins import _plugin_manager

    c, srv, auth = env
    catalog = (await c.get("/api/plugins/market", headers=auth)).json()
    plugin = next(p["id"] for p in catalog if not p.get("error"))
    installed = await c.post(f"/api/plugins/market/{plugin}/install", headers=auth)
    assert installed.status_code == 200, installed.text
    page = _plugin_manager(srv).plugin_dir(plugin) / "ui" / "scope-page.js"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("export {}", encoding="utf-8")
    cust = await _workbench_account(c, auth, plugins=(plugin,))

    for path in ("scope-page.js", "ui/scope-page.js"):
        ok = await c.get(f"/api/plugins/{plugin}/ui/{path}", headers=cust)
        assert ok.status_code == 200, (path, ok.text)
    # The manifest (and any source) sits outside ui/: admins only.
    source = f"/api/plugins/{plugin}/ui/plugin.yaml"
    assert (await c.get(source, headers=cust)).status_code == 404
    assert (await c.get(source, headers=auth)).status_code == 200


async def test_a_role_that_is_gone_or_unreadable_fails_closed(env):
    from octop.infra.db.repos.user_roles import UserRoleRepo
    from octop.infra.users.api_scope import role_api_scope

    c, srv, auth = env
    cust = await _workbench_account(c, auth)
    db = srv.services.db
    assert role_api_scope(db, SimpleNamespace(is_admin=False, role="gone")) == "none"
    assert role_api_scope(db, SimpleNamespace(is_admin=False, role="user")) is None

    role = UserRoleRepo(db).get_by_name("客户·选剧台")
    assert role is not None
    UserRoleRepo(db).update(
        role.user_role_id,
        user_role_name=role.user_role_name,
        description=role.description,
        permissions=role.permissions,
        policies=[(n, v) for n, v in role.policies if n != "api_scope"] + [("api_scope", "x")],
    )
    assert (await c.get("/api/auth/me", headers=cust)).status_code == 403
    assert (await c.post("/api/auth/logout", headers=cust)).status_code < 400


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


async def test_a_handshake_with_many_tokens_is_refused_unread(monkeypatch):
    guard, reached, sent = _guard(monkeypatch, scoped=set())
    resolved: list[str] = []
    real = api_scope.resolve_user_from_token_async

    async def counting(server: Any, token: str) -> Any:
        resolved.append(token)
        return await real(server, token)

    monkeypatch.setattr(api_scope, "resolve_user_from_token_async", counting)
    query = "&".join(f"token=t{i}" for i in range(50)).encode()
    ws = {"type": "websocket", "path": "/api/notifications/ws", "query_string": query}
    await guard(ws, None, _sink(sent))
    assert reached == [] and resolved == [] and sent[0]["code"] == 4003


async def test_an_open_socket_closes_once_its_account_is_scoped(monkeypatch):
    scoped: set[str] = set()
    guard, _reached, sent = _guard(monkeypatch, scoped=scoped)
    monkeypatch.setattr(api_scope, "_RECHECK_SECONDS", 0.0)
    seen: list[dict[str, Any]] = []

    async def app(_scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen.append(await receive())  # still allowed
        scoped.add("good")  # an admin moves the account into a scoped role
        seen.append(await receive())
        await send({"type": "websocket.send", "text": "late"})  # dropped after the close

    guard.app = app
    inbox = [{"type": "websocket.receive", "text": "a"}, {"type": "websocket.receive", "text": "b"}]

    async def receive() -> dict[str, Any]:
        return inbox.pop(0)

    ws = {"type": "websocket", "path": "/api/agents/a/terminal/ws", "query_string": b"token=good"}
    await guard(ws, receive, _sink(sent))
    assert seen == [
        {"type": "websocket.receive", "text": "a"},
        {"type": "websocket.disconnect", "code": 4003},
    ]
    assert sent == [{"type": "websocket.close", "code": 4003, "reason": "forbidden"}]


async def test_a_push_only_socket_closes_once_its_account_is_scoped(monkeypatch):
    """The dashboard never writes on the notification socket, so pushes trigger the check too."""
    scoped: set[str] = set()
    guard, _reached, sent = _guard(monkeypatch, scoped=scoped)
    monkeypatch.setattr(api_scope, "_RECHECK_SECONDS", 0.0)
    seen: list[dict[str, Any]] = []

    async def app(_scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "websocket.send", "text": "first"})
        scoped.add("good")
        await send({"type": "websocket.send", "text": "second"})  # not delivered
        seen.append(await receive())

    guard.app = app

    async def receive() -> dict[str, Any]:
        raise AssertionError("the client sends nothing")

    ws = {"type": "websocket", "path": "/api/notifications/ws", "query_string": b"token=good"}
    await guard(ws, receive, _sink(sent))
    assert sent == [
        {"type": "websocket.send", "text": "first"},
        {"type": "websocket.close", "code": 4003, "reason": "forbidden"},
    ]
    assert seen == [{"type": "websocket.disconnect", "code": 4003}]


async def test_an_open_socket_is_rechecked_after_its_token_expires(monkeypatch):
    """The recheck reloads the account verified at the handshake, not the token."""
    from octop.infra.errors import ErrorCode, OctopError

    scoped: set[str] = set()
    guard, _reached, sent = _guard(monkeypatch, scoped=scoped)
    monkeypatch.setattr(api_scope, "_RECHECK_SECONDS", 0.0)

    async def expired(_server: Any, _token: str) -> Any:
        raise OctopError(ErrorCode.AUTH_FAILED, "token expired")

    async def app(_scope: dict[str, Any], _receive: Any, send: Any) -> None:
        monkeypatch.setattr(api_scope, "resolve_user_from_token_async", expired)
        scoped.add("good")
        await send({"type": "websocket.send", "text": "not delivered"})

    guard.app = app
    ws = {"type": "websocket", "path": "/api/notifications/ws", "query_string": b"token=good"}
    await guard(ws, None, _sink(sent))
    assert sent == [{"type": "websocket.close", "code": 4003, "reason": "forbidden"}]


async def test_traffic_waits_for_a_recheck_in_progress(monkeypatch):
    """A receive that arrives while a send is re-checking is not let through."""
    import asyncio

    scoped: set[str] = set()
    guard, _reached, sent = _guard(monkeypatch, scoped=scoped)
    monkeypatch.setattr(api_scope, "_RECHECK_SECONDS", 0.05)
    checking, release = asyncio.Event(), asyncio.Event()
    seen: list[dict[str, Any]] = []

    async def app(_scope: dict[str, Any], receive: Any, send: Any) -> None:
        real = guard._any_scoped

        async def slow(users: list[Any]) -> bool:
            checking.set()
            await release.wait()
            scoped.add("good")  # the account was moved while the check ran
            return await real(users)

        monkeypatch.setattr(guard, "_any_scoped", slow)
        await asyncio.sleep(0.06)
        writer = asyncio.create_task(send({"type": "websocket.send", "text": "out"}))
        await checking.wait()
        reader = asyncio.create_task(receive())  # inside the interval the check just started
        await asyncio.sleep(0)
        release.set()
        await writer
        seen.append(await reader)

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.receive", "text": "ls"}

    guard.app = app
    ws = {"type": "websocket", "path": "/api/agents/a/terminal/ws", "query_string": b"token=good"}
    await guard(ws, receive, _sink(sent))
    assert sent == [{"type": "websocket.close", "code": 4003, "reason": "forbidden"}]
    assert seen == [{"type": "websocket.disconnect", "code": 4003}]


def _guard(monkeypatch: Any, *, scoped: set[str]) -> tuple[Any, list[str], list[dict[str, Any]]]:
    from octop.infra.errors import ErrorCode, OctopError

    accounts: dict[int, Any] = {}

    async def resolve(_server: Any, token: str) -> Any:
        if token == "bad":
            raise OctopError(ErrorCode.AUTH_FAILED, "invalid token")
        user = SimpleNamespace(id=len(accounts) + 1, is_admin=False, role="r", token=token)
        accounts[user.id] = user
        return user

    monkeypatch.setattr(api_scope, "resolve_user_from_token_async", resolve)
    monkeypatch.setattr(
        api_scope, "scope_of", lambda _server, user: "workbench" if user.token in scoped else None
    )
    reached: list[str] = []

    async def app(asgi_scope: dict[str, Any], _receive: Any, _send: Any) -> None:
        reached.append(asgi_scope["path"])

    server = SimpleNamespace(user_manager=SimpleNamespace(get_by_id=accounts.get))
    return api_scope.WebSocketScopeGuard(app, server=server), reached, []


def _sink(sent: list[dict[str, Any]]) -> Any:
    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    return send
