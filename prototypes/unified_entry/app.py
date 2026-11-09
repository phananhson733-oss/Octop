"""Offline G0 native-password entry. Not production-mounted; fixed test routes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

import anyio
import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.types import Receive, Scope, Send
from websockets.asyncio.client import connect as connect_websocket
from websockets.exceptions import ConnectionClosed

from octop import __version__
from octop.api.routers.i18n import router as i18n_router
from octop.infra.auth.sessions import REVOCATION_RECEIPT_HEADER
from octop.infra.unified_entry.admin import (
    AdminOperation,
    ControlTarget,
    OperationConflict,
    dispatch,
    normalize_desired,
    reconcile_admin_operations,
    signed_request,
    submit,
)
from octop.infra.unified_entry.reconcile import reconcile_revocations
from octop.infra.unified_entry.store import Binding, EntryStore, Session

RECONCILE_INTERVAL_SECONDS = 5.0
logger = logging.getLogger(__name__)


class FixedBackendWebSocket(connect_websocket):
    def process_redirect(self, exc: Exception) -> Exception | str:
        # Never resend native Authorization to another path or origin.
        return exc


class BackendResponse(StreamingResponse):
    def __init__(self, content: Any, upstream: httpx.Response, **kwargs: Any):
        super().__init__(content, **kwargs)
        self.upstream = upstream

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Also runs when headers fail before body iteration begins.
            with anyio.CancelScope(shield=True):
                await self.upstream.aclose()


class Login(BaseModel):
    username: str
    password: str
    captcha_token: str | None = None


class AccountSelection(BaseModel):
    account: str = Field(description="Bound account alias, e.g. ``customer/same_name``.")
    role: str = Field(min_length=1)
    templates: list[str]
    max_agents: int | None = Field(ge=0, description="Expert limit; null means unlimited.")


class AccountGrants(AccountSelection):
    operation_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="Client-chosen idempotency key; resubmitting it never creates a second change.",
    )


def build_candidate(
    clients: dict[str, httpx.AsyncClient],
    bindings: dict[str, Binding],
    store: EntryStore,
    dashboard: Path | None = None,
    control: Mapping[str, ControlTarget] | None = None,
) -> FastAPI:
    """Bootstrap mapping conflicts fail rather than overwrite an existing identity.

    ``control`` maps an instance name to its signing key; without it the admin routes are off.
    """
    for alias, binding in bindings.items():
        store.register(alias, binding)
    targets = dict(control or {})

    async def retry_loop(stop: asyncio.Event) -> None:
        cursor = ""
        admin_cursor = 0
        while not stop.is_set():
            try:
                result = await reconcile_revocations(store, clients, cursor)
                cursor = result.cursor
            except Exception:
                # Never log backend credentials, responses, or database contents.
                logger.warning("Entry revocation pass failed; pending work retained")
            if targets:
                try:
                    admin_pass = await reconcile_admin_operations(
                        store.operations, clients, targets, admin_cursor
                    )
                    admin_cursor = admin_pass.cursor
                except Exception:
                    logger.warning("Entry admin operation pass failed; pending work retained")
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=RECONCILE_INTERVAL_SECONDS)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        stop = asyncio.Event()
        task = asyncio.create_task(retry_loop(stop), name="entry-revocation-retry")
        try:
            yield
        finally:
            # Complete the active pass, including executor work, before the DB owner closes it.
            stop.set()
            await task

    app = FastAPI(lifespan=lifespan)
    app.include_router(i18n_router, prefix="/api")

    async def io(function: Any, *args: Any) -> Any:
        future = asyncio.get_running_loop().run_in_executor(None, function, *args)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # A canceled coroutine must not outlive its database work at shutdown.
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not future.cancelled():
                future.exception()
            raise

    def deny() -> HTTPException:
        return HTTPException(status_code=401, detail="Authentication required")

    def token_of(request: Request) -> str:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise deny()
        return token

    async def authenticated(request: Request) -> tuple[str, Session, dict]:
        token = token_of(request)
        session = await io(store.read, token)
        if session is None:
            raise deny()
        response = await clients[session.binding.instance].get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {session.backend_token}"},
            follow_redirects=False,
        )
        if response.status_code != 200:
            await io(store.begin_revoke, token)
            raise deny()
        user = response.json()
        if user["id"] != session.binding.user_id:
            await io(store.begin_revoke, token)
            raise deny()
        renewed = response.headers.get("X-Octop-Access-Token")
        if renewed:
            await io(store.renew_backend, token, renewed)
        refreshed = await io(store.read, token)
        if refreshed is None:
            raise deny()
        return token, refreshed, user

    @app.get("/__candidate")
    async def candidate_marker() -> dict:
        return {"candidate": "pick-test1-unification", "synthetic": True}

    @app.get("/api/setup/status")
    async def setup_status() -> dict:
        # Entry has no first-run provisioning API; bindings are administrator-managed.
        return {
            "setup_required": False,
            "setup_done": True,
            "enabled": True,
            "has_password": True,
            "database_bound": True,
            "wizard_password_exists": False,
            "wizard_password_required": False,
        }

    @app.get("/api/auth/oidc/status")
    @app.get("/api/auth/ldap/status")
    async def external_login_status() -> dict:
        return {"enabled": False, "display_name": ""}

    @app.get("/api/auth/oauth/status")
    async def oauth_status() -> dict:
        return {"providers": []}

    @app.get("/api/auth/captcha")
    async def captcha_status() -> dict:
        # Candidate only. A production entry must supply its own configured challenge.
        return {"provider": "slider"}

    @app.get("/api/update/status")
    async def managed_update_status(request: Request) -> dict:
        await authenticated(request)
        # Entry deployment owns updates; do not trigger native PyPI probes/upgrades.
        return {
            "current_version": __version__,
            "latest_version": None,
            "has_update": False,
            "is_editable": True,
            "service_mode": None,
            "error": None,
            "error_code": None,
            "source": None,
            "last_check_time": None,
            "release_notes": None,
        }

    @app.post("/api/auth/login")
    async def login(body: Login) -> dict:
        binding = await io(store.binding, body.username)
        if binding is None:
            raise deny()
        result = await clients[binding.instance].post(
            "/api/auth/login",
            json={
                "username": binding.username,
                "password": body.password,
                "captcha_token": body.captcha_token,
            },
            follow_redirects=False,
        )
        if result.status_code != 200:
            raise deny()
        data = result.json()
        if data["user"]["id"] != binding.user_id:
            raise deny()
        try:
            token = await io(store.create, body.username, data["access_token"], data["expires_in"])
        except ValueError:
            raise deny() from None
        return {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": data["expires_in"],
            "identity_id": binding.identity_id,
            "user": {
                **data["user"],
                "identity_id": binding.identity_id,
                "identity_scope": binding.storage_scope,
            },
        }

    @app.get("/api/auth/me")
    async def me(request: Request) -> dict:
        _token, session, user = await authenticated(request)
        return {
            **user,
            "identity_id": session.binding.identity_id,
            "identity_scope": session.binding.storage_scope,
        }

    @app.get("/api/plugins")
    async def plugins(request: Request) -> Response:
        token, session, _user = await authenticated(request)
        result = await clients[session.binding.instance].get(
            "/api/plugins",
            headers={"Authorization": f"Bearer {session.backend_token}"},
            follow_redirects=False,
        )
        if await io(store.read, token) is None:
            raise deny()
        return Response(
            result.content, status_code=result.status_code, media_type="application/json"
        )

    @app.get("/api/plugins/{plugin_id}/ui/{file_path:path}")
    async def plugin_asset(plugin_id: str, file_path: str, request: Request) -> Response:
        token, session, _user = await authenticated(request)
        if any(part in {".", ".."} for part in file_path.split("/")) or "\\" in file_path:
            raise HTTPException(400, "Invalid asset path")
        path = f"/api/plugins/{quote(plugin_id, safe='')}/ui/{quote(file_path, safe='/')}"
        headers = {"Authorization": f"Bearer {session.backend_token}"}
        for name in ("if-none-match", "range", "if-range"):
            if name in request.headers:
                headers[name] = request.headers[name]
        result = await clients[session.binding.instance].get(
            path, headers=headers, follow_redirects=False
        )
        if await io(store.read, token) is None:
            raise deny()
        safe_headers = {
            name: value
            for name, value in result.headers.items()
            if name
            in {
                "content-type",
                "content-disposition",
                "cache-control",
                "etag",
                "content-range",
                "accept-ranges",
            }
        }
        return Response(result.content, status_code=result.status_code, headers=safe_headers)

    @app.post("/api/auth/logout", status_code=204)
    async def logout(request: Request) -> Response:
        token = token_of(request)
        session = await io(store.begin_revoke, token)
        if session is None:
            raise deny()
        try:
            result = await clients[session.binding.instance].post(
                "/api/auth/session/revoke",
                headers={"Authorization": f"Bearer {session.backend_token}"},
                follow_redirects=False,
            )
        except httpx.RequestError:
            raise HTTPException(502, "Entry session revoked; backend revocation pending") from None
        if result.status_code != 204 or result.headers.get(REVOCATION_RECEIPT_HEADER) != "1":
            raise HTTPException(502, "Entry session revoked; backend revocation pending")
        await io(store.finish_revoke, token)
        return Response(status_code=204)

    @app.websocket("/api/agents/{agent_id}/chat/ws")
    async def chat_socket(websocket: WebSocket, agent_id: str) -> None:
        raw = websocket.headers.get("authorization", "")
        token = (
            raw[7:].strip()
            if raw.lower().startswith("bearer ")
            else websocket.query_params.get("token", "")
        )
        session = await io(store.read, token) if token else None
        if session is None:
            await websocket.close(code=4001)
            return
        client = clients[session.binding.instance]
        # Never copy the client's URL, tenant headers, cookies or native JWT query.
        url = client.base_url.copy_with(
            scheme="wss" if client.base_url.scheme == "https" else "ws",
            path=f"/api/agents/{quote(agent_id, safe='')}/chat/ws",
            query=None,
        )
        tasks: list[asyncio.Task] = []
        code = 1000
        try:
            async with FixedBackendWebSocket(
                str(url),
                additional_headers={"Authorization": f"Bearer {session.backend_token}"},
                proxy=None,
                open_timeout=10,
                close_timeout=5,
                max_size=16 * 1024 * 1024,
            ) as backend:
                await websocket.accept()

                async def check() -> None:
                    current = await io(store.read, token)
                    if current is None:
                        raise PermissionError("Entry session revoked")
                    profile = await client.get(
                        "/api/auth/me",
                        headers={"Authorization": f"Bearer {current.backend_token}"},
                        follow_redirects=False,
                    )
                    if (
                        profile.status_code != 200
                        or profile.json().get("id") != current.binding.user_id
                    ):
                        await io(store.begin_revoke, token)
                        raise PermissionError("Backend session unavailable")
                    renewed = profile.headers.get("X-Octop-Access-Token")
                    if renewed:
                        await io(store.renew_backend, token, renewed)
                    if await io(store.read, token) is None:
                        raise PermissionError("Entry session revoked")

                async def incoming_frames() -> None:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        await check()
                        if message.get("text") is not None:
                            await backend.send(message["text"])
                        elif message.get("bytes") is not None:
                            await backend.send(message["bytes"])

                async def outgoing_frames() -> None:
                    async for message in backend:
                        await check()
                        if isinstance(message, str):
                            await websocket.send_text(message)
                        else:
                            await websocket.send_bytes(message)

                async def watch() -> None:
                    while True:
                        await asyncio.sleep(1)
                        await check()

                tasks = [
                    asyncio.create_task(fn(), name=f"entry-ws-{fn.__name__}")
                    for fn in (incoming_frames, outgoing_frames, watch)
                ]
                done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        except PermissionError:
            code = 4001
        except (ConnectionClosed, WebSocketDisconnect):
            pass
        except Exception:
            # Connection exceptions can contain upstream URLs/headers; never echo them.
            code = 1011
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            with suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(code=code)

    async def platform_admin(request: Request) -> Session:
        if not targets:
            raise HTTPException(404, "Route not exposed by entry")
        _token, session, _user = await authenticated(request)
        if not await io(store.is_platform_admin, session.binding.identity_id):
            raise HTTPException(403, "Platform administrator required")
        return session

    async def managed_account(alias: str) -> Binding:
        binding = await io(store.binding, alias)
        if binding is None or binding.instance not in targets or binding.instance not in clients:
            raise HTTPException(404, "Account not found")
        return binding

    @app.get("/api/admin/accounts")
    async def admin_accounts(request: Request) -> list[dict]:
        await platform_admin(request)
        rows = []
        for alias in sorted(bindings):
            binding = await io(store.binding, alias)
            if binding is None:
                continue
            latest = await io(store.operations.latest_for_account, alias)
            rows.append(
                {
                    "account": alias,
                    "identity_id": binding.identity_id,
                    "instance": binding.instance,
                    "user_id": binding.user_id,
                    "manageable": binding.instance in targets,
                    "latest_operation": (
                        asdict(AdminOperation.from_row(latest)) if latest is not None else None
                    ),
                }
            )
        return rows

    @app.post("/api/admin/accounts/preview")
    async def admin_preview(body: AccountSelection, request: Request) -> Response:
        await platform_admin(request)
        binding = await managed_account(body.account)
        result = await signed_request(
            clients[binding.instance],
            targets[binding.instance],
            "POST",
            f"/api/control/users/{binding.user_id}/preview",
            normalize_desired(body.role, body.templates, body.max_agents),
        )
        return Response(
            result.content, status_code=result.status_code, media_type="application/json"
        )

    @app.put("/api/admin/accounts/grants")
    async def admin_apply(body: AccountGrants, request: Request) -> dict:
        await platform_admin(request)
        binding = await managed_account(body.account)
        try:
            operation = await submit(
                store.operations,
                account=body.account,
                instance=binding.instance,
                user_id=binding.user_id,
                desired=normalize_desired(body.role, body.templates, body.max_agents),
                operation_id=body.operation_id,
                client=clients[binding.instance],
                target=targets[binding.instance],
            )
        except OperationConflict as exc:
            raise HTTPException(409, str(exc)) from None
        return asdict(operation)

    @app.get("/api/admin/operations/{operation_id}")
    async def admin_operation(operation_id: str, request: Request) -> dict:
        await platform_admin(request)
        row = await io(store.operations.get, operation_id)
        if row is None:
            raise HTTPException(404, "Operation not found")
        return asdict(AdminOperation.from_row(row))

    @app.post("/api/admin/operations/{operation_id}/retry")
    async def admin_retry(operation_id: str, request: Request) -> dict:
        await platform_admin(request)
        row = await io(store.operations.get, operation_id)
        if row is None or row.instance not in targets or row.instance not in clients:
            raise HTTPException(404, "Operation not found")
        if row.state == "rejected":
            raise HTTPException(409, "Rejected operations are not retried; submit a new change")
        if row.state == "partial":
            await io(store.operations.requeue, operation_id)
        row = await io(store.operations.get, operation_id)
        assert row is not None
        if row.state != "pending":
            return asdict(AdminOperation.from_row(row))
        operation = await dispatch(
            store.operations,
            AdminOperation.from_row(row),
            clients[row.instance],
            targets[row.instance],
        )
        return asdict(operation)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
    async def business_api(path: str, request: Request) -> Response:
        # Do not proxy bootstrap/auth/bridge/system administration or arbitrary hosts.
        allowed_roots = {
            "agents",
            "experts",
            "tasks",
            "threads",
            "uploads",
            "downloads",
            "knowledge-bases",
            "skills",
            "plugins",
        }
        readonly = {
            "settings/timezone",
            "settings/capabilities",
            "settings/upload",
            "settings/hitl",
            "models",
            "providers/resolved",
            "providers/active-model",
            "voice/active",
            "slash/commands",
            "connector-instances",
            "bridge/connections",
            "browser/record-replay/status",
        }
        if path.split("/", 1)[0] not in allowed_roots and not (
            (request.method == "GET" and path in readonly)
            or (path == "preferences" and request.method in {"GET", "PATCH"})
            or (path == "auth/me" and request.method == "PATCH")
            or (path == "auth/change-password" and request.method == "POST")
        ):
            raise HTTPException(404, "Route not exposed by entry")
        if any(part in {".", ".."} for part in path.split("/")) or "\\" in path:
            raise HTTPException(400, "Invalid API path")
        token, session, _user = await authenticated(request)
        client = clients[session.binding.instance]
        headers = {"Authorization": f"Bearer {session.backend_token}"}
        for name in (
            "accept",
            "accept-language",
            "content-type",
            "range",
            "if-range",
            "if-none-match",
            "last-event-id",
            "x-octop-agent-id",
        ):
            if name in request.headers:
                headers[name] = request.headers[name]
        params = [
            (name, value)
            for name, value in request.query_params.multi_items()
            if name not in {"access_token", "instance", "tenant_id"}
        ]

        async def incoming():
            async for chunk in request.stream():
                if await io(store.read, token) is None:
                    # Raise rather than EOF: truncated uploads must not look complete.
                    raise deny()
                yield chunk

        outbound = client.build_request(
            request.method,
            "/api/" + quote(path, safe="/"),
            params=params,
            headers=headers,
            content=incoming(),
        )
        result = await client.send(outbound, stream=True, follow_redirects=False)
        handed_off = False
        try:
            if 300 <= result.status_code < 400 and result.status_code != 304:
                raise HTTPException(502, "Backend redirect is not exposed")
            if await io(store.read, token) is None:
                raise deny()
            renewed = result.headers.get("X-Octop-Access-Token")
            if renewed:
                await io(store.renew_backend, token, renewed)
            if path == "auth/me" and result.status_code == 200:
                await result.aread()
                profile = result.json()
                if (
                    profile.get("id") != session.binding.user_id
                    or await io(store.read, token) is None
                ):
                    raise deny()
                return JSONResponse(
                    {
                        **profile,
                        "identity_id": session.binding.identity_id,
                        "identity_scope": session.binding.storage_scope,
                    }
                )
            safe_headers = {
                name: value
                for name, value in result.headers.items()
                if name
                in {
                    "content-type",
                    "content-disposition",
                    "cache-control",
                    "etag",
                    "content-range",
                    "accept-ranges",
                }
            }

            async def body():
                async for chunk in result.aiter_bytes():
                    if await io(store.read, token) is None:
                        break
                    yield chunk

            response = BackendResponse(
                body(),
                upstream=result,
                status_code=result.status_code,
                headers=safe_headers,
            )
            handed_off = True
            return response
        finally:
            if not handed_off:
                with anyio.CancelScope(shield=True):
                    await result.aclose()

    if dashboard is not None:
        static_root = dashboard.resolve()
        if not (static_root / "index.html").is_file():
            raise ValueError("Build the dashboard before running this candidate")

        @app.get("/{path:path}")
        async def dashboard_file(path: str) -> FileResponse:
            target = (static_root / path).resolve()
            if not target.is_relative_to(static_root):
                raise HTTPException(404)
            if target.is_file():
                return FileResponse(target)
            return FileResponse(static_root / "index.html")

    return app
