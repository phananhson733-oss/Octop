"""Enforce a role's ``api_scope`` policy (``octop.infra.users.api_scope``).

An account whose role has a scope may call only the routes listed for it here;
everything else under ``/api`` answers 403, and every ``/api`` WebSocket is
closed before it is accepted. JWT-exempt routes (login, health, i18n) are not
affected. Route-level checks such as ``allowed_plugin_ui`` still apply on top.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import parse_qs

from octop.api.deps import (
    extract_raw_token,
    is_jwt_exempt_path,
    resolve_user_from_token_async,
)
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.api_scope import API_SCOPE_WORKBENCH, role_api_scope

_Route = tuple[str, re.Pattern[str]]


def _route(method: str, pattern: str) -> _Route:
    return method, re.compile(pattern)


# Own account, session and preferences (language, sidebar layout), the host
# capability flags the shell reads on load, the plugin list (filtered by
# ``allowed_plugin_ui``), the plugin pages' files, and the server timezone the
# pages format dates with.
_SIGN_OUT = _route("POST", r"/api/auth/logout")
_ACCOUNT: tuple[_Route, ...] = (
    _route("GET", r"/api/auth/me"),
    _route("PATCH", r"/api/auth/me"),
    _route("POST", r"/api/auth/change-password"),
    _route("POST", r"/api/auth/me/avatar"),
    _route("DELETE", r"/api/auth/me/avatar"),
    # The route itself serves only the caller's own portrait without ``users``.
    _route("GET", r"/api/users/\d+/avatar"),
    _route("GET", r"/api/preferences"),
    _route("PATCH", r"/api/preferences"),
    _route("GET", r"/api/settings/capabilities"),
    _SIGN_OUT,
)
_ROUTES: dict[str, tuple[_Route, ...]] = {
    API_SCOPE_WORKBENCH: (
        *_ACCOUNT,
        _route("GET", r"/api/plugins"),
        _route("GET", r"/api/plugins/[^/]+/ui/.+"),
        _route("GET", r"/api/settings/timezone"),
    ),
}


def scope_allows(scope: str, method: str, path: str) -> bool:
    """Whether ``scope`` reaches the route. An unknown scope may only sign out."""
    method = "GET" if method == "HEAD" else method
    return any(
        method == allowed and pattern.fullmatch(path)
        for allowed, pattern in _ROUTES.get(scope, (_SIGN_OUT,))
    )


def scope_of(server: Any, user: Any) -> str | None:
    assert server.services is not None
    return role_api_scope(server.services.db, user)


def forbidden() -> OctopError:
    return OctopError(ErrorCode.FORBIDDEN, "not available to this account", status=403)


# Real clients send one token, sometimes twice (query and header). Each one costs
# a database lookup before the route runs, so a handshake with more is refused.
_MAX_CREDENTIALS = 3
# An open socket re-checks its account at most this often, on the next message it
# receives, so moving an account into a scoped role also cuts its open sockets.
_RECHECK_SECONDS = 30.0


class WebSocketScopeGuard:
    """Close ``/api`` WebSockets of scoped accounts; HTTP is checked in ``jwt_auth``.

    WebSocket routes authenticate inside their handlers, so this resolves the
    token itself (``token`` / ``access_token`` query parameter or a Bearer
    header). A missing or bad token is left to the route, which rejects it.
    Refusing before ``accept`` reaches the client as an HTTP 403 handshake reply.
    Once open, the socket is re-checked on traffic either way (see
    ``_RECHECK_SECONDS``) and closed with 4003 if the account became scoped.
    The desktop and mobile streams take their token from the first message
    instead; they also need the desktop / mobile permission keys.
    """

    def __init__(self, app: Any, server: Any) -> None:
        self.app = app
        self.server = server

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        if (
            scope["type"] == "websocket"
            and path.startswith("/api/")
            and not is_jwt_exempt_path(path)
        ):
            tokens = _credentials(scope)
            if len(tokens) > _MAX_CREDENTIALS:
                await send({"type": "websocket.close", "code": 4003, "reason": "forbidden"})
                return
            users = await self._users(tokens)
            if await self._any_scoped(users):
                await send({"type": "websocket.close", "code": 4003, "reason": "forbidden"})
                return
            if users:
                receive, send = self._rechecking([user.id for user in users], receive, send)
        await self.app(scope, receive, send)

    async def _users(self, tokens: list[str]) -> list[Any]:
        """The accounts behind the tokens that verify; a bad one is left to the route."""
        users = []
        for token in tokens:
            try:
                users.append(await resolve_user_from_token_async(self.server, token))
            except OctopError:
                continue
        return users

    async def _any_scoped(self, users: list[Any]) -> bool:
        loop = asyncio.get_running_loop()
        for user in users:
            if await loop.run_in_executor(None, scope_of, self.server, user) is not None:
                return True
        return False

    def _rechecking(self, user_ids: list[int], receive: Any, send: Any) -> tuple[Any, Any]:
        """Re-check the account on traffic in either direction, at most every ``_RECHECK_SECONDS``.

        The accounts verified at the handshake are reloaded by id, so a token
        that has since expired does not stop the check. Both directions count:
        some sockets only push (the dashboard sends nothing on the
        notification socket). Once the guard closes the socket
        the route only sees a disconnect, and its own later sends (a farewell
        frame, its own close) are dropped, since the server would refuse them.
        """
        checked = time.monotonic()
        closed = False
        # Traffic in the other direction waits for a check in progress instead of
        # passing while it runs (the terminal reads and writes from two tasks).
        lock = asyncio.Lock()

        async def close_if_scoped() -> bool:
            nonlocal checked, closed
            async with lock:
                if closed or time.monotonic() - checked < _RECHECK_SECONDS:
                    return closed
                users = [self.server.user_manager.get_by_id(user_id) for user_id in user_ids]
                if await self._any_scoped([u for u in users if u is not None]):
                    closed = True
                    await send({"type": "websocket.close", "code": 4003, "reason": "forbidden"})
                checked = time.monotonic()
                return closed

        async def send_checked(message: dict[str, Any]) -> None:
            if message.get("type") == "websocket.send" and await close_if_scoped():
                return
            if not closed:
                await send(message)

        async def receive_checked() -> dict[str, Any]:
            if closed:
                return {"type": "websocket.disconnect", "code": 4003}
            message: dict[str, Any] = await receive()
            if message.get("type") == "websocket.receive" and await close_if_scoped():
                return {"type": "websocket.disconnect", "code": 4003}
            return message

        return receive_checked, send_checked


def _credentials(scope: dict[str, Any]) -> list[str]:
    """Every token the request carries, in any place a WebSocket route reads one.

    Routes differ (query only, Bearer first, last repeated parameter wins), so
    the guard checks them all: a bad first token must not wave a good second
    one through.
    """
    query = parse_qs(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
    tokens = [*query.get("token", []), *query.get("access_token", [])]
    for name, value in scope.get("headers") or []:
        if name.lower() == b"authorization":
            bearer = extract_raw_token(authorization=value.decode("latin-1"))
            if bearer:
                tokens.append(bearer)
    return list(dict.fromkeys(t.strip() for t in tokens if t.strip()))
