"""One entry, two real Octop ASGI instances, native passwords and isolated DBs."""

import json
from contextlib import AsyncExitStack

import httpx
import pytest
from prototypes.unified_entry.app import Binding, build_candidate

from tests.support.app import octop_client
from tests.support.auth import TEST_PASSWORD, bearer, bootstrap_admin


@pytest.fixture
def entry_store_factory(tmp_path):
    from cryptography.fernet import Fernet

    from octop.config import OctopConfig
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.services import build_shared_services
    from octop.infra.unified_entry.store import EntryStore
    from octop.infra.utils.paths import PathLayout

    paths = PathLayout(tmp_path / "entry-state")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    db.close()
    key = Fernet.generate_key()
    pools = []

    def open_store():
        pool = SqlitePool(paths.db)
        pools.append(pool)
        services = build_shared_services(db=pool, paths=paths, config=OctopConfig())
        return EntryStore(services, key)

    yield open_store
    for pool in pools:
        pool.close()


@pytest.fixture
def entry_store(entry_store_factory):
    return entry_store_factory()


async def test_native_entry_routes_same_named_accounts_without_password_fanout(
    tmp_path, entry_store
):
    async with AsyncExitStack() as stack:
        clients = {}
        servers = {}
        bindings = {}
        admin_headers = {}
        passwords = {"internal": "InternalPass12", "customer": "CustomerPass34"}
        for name in ("internal", "customer"):
            home = tmp_path / name
            client, server = await stack.enter_async_context(octop_client(home))
            await bootstrap_admin(client, home)
            admin = (
                await client.post(
                    "/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD}
                )
            ).json()
            headers = bearer(admin["access_token"])
            allowed_plugins = []
            if name == "internal":
                catalog = (await client.get("/api/plugins/market", headers=headers)).json()
                internal_plugin = next(row["id"] for row in catalog if not row.get("error"))
                installed = await client.post(
                    f"/api/plugins/market/{internal_plugin}/install", headers=headers
                )
                assert installed.status_code == 200, installed.text
                allowed_plugins = [internal_plugin]
                data_dir = server.plugin_manager.plugin_dir(internal_plugin) / "ui" / "data"
                data_dir.mkdir(parents=True, exist_ok=True)
                (data_dir / "internal.json").write_text('{"internal_only": true}')

            role = await client.post(
                "/api/users/roles",
                headers=headers,
                json={
                    "user_role_name": "Bound role",
                    "permissions": [],
                    "policies": [
                        {"name": "allowed_plugin_ui", "value": json.dumps(allowed_plugins)}
                    ],
                },
            )
            result = await client.post(
                "/api/users",
                headers=headers,
                json={
                    "username": "same_name",
                    "password": passwords[name],
                    "role": role.json()["user_role_id"],
                },
            )
            assert result.status_code == 201, result.text
            server.services.agent_repo.create(
                agent_id="same-agent", user_id=result.json()["id"], name=name + " expert"
            )

            bindings[name + "/same_name"] = Binding(
                "identity-" + name, name, "same_name", result.json()["id"]
            )
            clients[name], servers[name], admin_headers[name] = client, server, headers
        app = build_candidate(clients, bindings, entry_store)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://candidate"
        ) as entry:
            # No unqualified collision resolution or trial against both backends.
            assert (
                await entry.post(
                    "/api/auth/login",
                    json={"username": "same_name", "password": passwords["internal"]},
                )
            ).status_code == 401
            wrong = await entry.post(
                "/api/auth/login",
                json={"username": "customer/same_name", "password": passwords["internal"]},
            )
            assert wrong.status_code == 401
            sessions = {}
            for name in ("internal", "customer"):
                logged = await entry.post(
                    "/api/auth/login",
                    json={"username": name + "/same_name", "password": passwords[name]},
                )
                assert logged.status_code == 200, logged.text
                assert logged.json()["user"]["username"] == "same_name"
                assert logged.json()["expires_in"] > 0

                token = logged.json()["access_token"]
                sessions[name] = bearer(token)
                assert (
                    await clients[name].get("/api/auth/me", headers=bearer(token))
                ).status_code == 401
                me = await entry.get("/api/auth/me", headers=bearer(token))
                assert me.json()["identity_id"] == "identity-" + name
                assert me.json()["username"] == "same_name"
                agent = await entry.get("/api/agents/same-agent", headers=bearer(token))
                assert agent.status_code == 200, agent.text
                assert agent.json()["name"] == name + " expert"

                assert "x-octop-access-token" not in me.headers
            # Attempting to choose another instance in a request cannot change binding.
            me = await entry.get(
                "/api/auth/me?instance=internal",
                headers={**sessions["customer"], "X-Tenant-ID": "internal"},
            )
            assert me.json()["identity_id"] == "identity-customer"
            assert (await entry.get("/api/plugins", headers=sessions["customer"])).json() == []
            internal_data = (await entry.get("/api/plugins", headers=sessions["internal"])).json()
            assert [item["id"] for item in internal_data] == [internal_plugin]
            spoofed = await entry.get(
                "/api/plugins?instance=internal",
                headers={**sessions["customer"], "X-Tenant-ID": "internal"},
            )
            assert spoofed.json() == []
            profile = await entry.patch(
                "/api/auth/me",
                headers=sessions["customer"],
                json={"display_name": "Customer Renamed"},
            )
            assert profile.status_code == 200, profile.text
            assert profile.json()["identity_id"] == "identity-customer"
            assert profile.json()["display_name"] == "Customer Renamed"
            internal_profile = (
                await entry.get("/api/auth/me", headers=sessions["internal"])
            ).json()
            assert internal_profile.get("display_name") != "Customer Renamed"

            assert (await entry.get("/api/bridge", headers=sessions["customer"])).status_code == 404
            assert (
                await entry.post("/api/setup/initial-admin", json={}, headers=sessions["customer"])
            ).status_code == 404

            asset_path = f"/api/plugins/{internal_plugin}/ui/ui/data/internal.json"
            asset = await entry.get(asset_path, headers=sessions["internal"])
            assert asset.status_code == 200, asset.text
            assert asset.json() == {"internal_only": True}
            denied_asset = await entry.get(
                asset_path,
                headers={
                    **sessions["customer"],
                    "If-None-Match": asset.headers["etag"],
                    "Range": "bytes=0-4",
                },
            )
            assert denied_asset.status_code == 403
            assert "internal_only" not in denied_asset.text

            assert (
                await entry.post("/api/auth/logout", headers=sessions["customer"])
            ).status_code == 204
            assert (
                await entry.get("/api/auth/me", headers=sessions["customer"])
            ).status_code == 401
            assert (
                await entry.get("/api/auth/me", headers=sessions["internal"])
            ).status_code == 200
            uid = bindings["internal/same_name"].user_id
            await clients["internal"].patch(
                f"/api/users/{uid}", headers=admin_headers["internal"], json={"disabled": True}
            )
            assert (
                await entry.get("/api/auth/me", headers=sessions["internal"])
            ).status_code == 401


@pytest.mark.parametrize("failure", [401, 500, 503, "timeout"])
async def test_logout_failure_denies_entry_access_and_allows_revocation_retry(failure, entry_store):
    attempts = []

    def backend(request):
        if request.url.path == "/api/auth/login":
            return httpx.Response(
                200, json={"access_token": "backend-only", "expires_in": 3600, "user": {"id": 7}}
            )
        if request.url.path == "/api/auth/session/revoke":
            attempts.append(request.url.path)
            if len(attempts) == 1:
                if failure == "timeout":
                    raise httpx.ReadTimeout("candidate timeout", request=request)
                return httpx.Response(
                    failure, json={"error": {"code": "TOKEN_EXPIRED"}} if failure == 401 else {}
                )
            return httpx.Response(204, headers={"X-Octop-Session-Revoked": "1"})
        raise AssertionError("Revoked entry session must not reach backend")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(backend), base_url="https://candidate-backend"
    ) as client:
        app = build_candidate(
            {"customer": client},
            {"customer/user": Binding("global-7", "customer", "user", 7)},
            entry_store,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://entry"
        ) as entry:
            result = await entry.post(
                "/api/auth/login", json={"username": "customer/user", "password": "test-only"}
            )
            headers = bearer(result.json()["access_token"])
            assert (await entry.post("/api/auth/logout", headers=headers)).status_code == 502
            assert (await entry.get("/api/auth/me", headers=headers)).status_code == 401
            assert (await entry.post("/api/auth/logout", headers=headers)).status_code == 204
            assert len(attempts) == 2


async def test_lost_backend_receipt_converges_after_retry(env, entry_store, entry_store_factory):
    client, _server, _auth = env

    class LostReceipt(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.ASGITransport(app=client._octop_app)
            self.lost = False

        async def handle_async_request(self, request):
            response = await self.inner.handle_async_request(request)
            if request.url.path == "/api/auth/session/revoke" and not self.lost:
                assert response.status_code == 204
                self.lost = True
                await response.aclose()
                raise httpx.ReadTimeout("receipt lost after commit", request=request)
            return response

    async with httpx.AsyncClient(transport=LostReceipt(), base_url="http://backend") as backend:
        app = build_candidate(
            {"internal": backend},
            {"admin": Binding("global-admin", "internal", "admin", 1)},
            entry_store,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://entry"
        ) as entry:
            logged = await entry.post(
                "/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD}
            )
            token = logged.json()["access_token"]
            native_token = entry_store.read(token).backend_token
            assert (await entry.post("/api/auth/logout", headers=bearer(token))).status_code == 502
            assert entry_store.read(token) is None
            assert entry_store.read(token, pending=True) is not None
            assert (
                await client.get("/api/auth/me", headers=bearer(native_token))
            ).status_code == 401
            from octop.infra.unified_entry.reconcile import reconcile_revocations

            # A fresh store/connection discovers pending work without a browser token.
            restarted = entry_store_factory()
            result = await reconcile_revocations(restarted, {"internal": backend})
            assert result.completed == 1 and result.pending == 0
            assert entry_store.read(token, pending=True) is None
            assert (await entry.get("/api/auth/me", headers=bearer(token))).status_code == 401
            assert (await reconcile_revocations(restarted, {"internal": backend})).completed == 0


async def test_candidate_lifespan_retries_pending_without_browser(env, entry_store, monkeypatch):
    import asyncio

    import prototypes.unified_entry.app as candidate_module

    client, _server, _auth = env
    login_response = await client.post(
        "/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD}
    )
    entry_store.register("admin", Binding("background-admin", "internal", "admin", 1))
    token = entry_store.create("admin", login_response.json()["access_token"], 3600)
    entry_store.begin_revoke(token)
    monkeypatch.setattr(candidate_module, "RECONCILE_INTERVAL_SECONDS", 0.01)
    app = build_candidate({"internal": client}, {}, entry_store)
    async with app.router.lifespan_context(app):

        async def until_complete():
            while await asyncio.to_thread(entry_store.read, token, True) is not None:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(until_complete(), timeout=5)
    assert not any(
        task.get_name() == "entry-revocation-retry" and not task.done()
        for task in asyncio.all_tasks()
    )


async def test_candidate_shutdown_waits_for_database_work(entry_store, monkeypatch):
    import asyncio
    import threading

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original = entry_store.pending_revocations

    def slow_scan(*args):
        started.set()
        release.wait(timeout=5)
        result = original(*args)
        finished.set()
        return result

    monkeypatch.setattr(entry_store, "pending_revocations", slow_scan)
    app = build_candidate({}, {}, entry_store)
    context = app.router.lifespan_context(app)
    await context.__aenter__()
    assert await asyncio.to_thread(started.wait, 2)
    shutdown = asyncio.create_task(context.__aexit__(None, None, None))
    try:
        await asyncio.sleep(0.03)
        assert not shutdown.done()
        assert not finished.is_set()
    finally:
        release.set()
        await asyncio.wait_for(shutdown, timeout=2)
    assert finished.is_set()


async def test_stream_proxy_rechecks_revocation_and_closes_backend(entry_store):
    import asyncio

    closed = []
    tokens = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"part": "allowed"}\n\n'
            await asyncio.to_thread(entry_store.begin_revoke, tokens[0])
            yield b'data: {"part": "must-not-deliver"}\n\n'

        async def aclose(self):
            closed.append(True)

    def backend(request):
        if request.url.path == "/api/auth/login":
            return httpx.Response(
                200,
                json={
                    "access_token": "native-secret",
                    "expires_in": 3600,
                    "user": {"id": 7, "username": "user"},
                },
            )
        if request.url.path == "/api/auth/me":
            assert request.headers["authorization"] == "Bearer native-secret"
            return httpx.Response(200, json={"id": 7, "username": "user"})
        assert request.url.path == "/api/agents/a/chat/hitl/resume"
        assert request.headers["authorization"] == "Bearer native-secret"
        assert "x-tenant-id" not in request.headers
        assert "access_token" not in request.url.params
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "X-Octop-Access-Token": "native-renewal",
                "Set-Cookie": "private=backend",
            },
            stream=Chunks(),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(backend), base_url="http://backend"
    ) as client:
        app = build_candidate(
            {"customer": client}, {"user": Binding("gid", "customer", "user", 7)}, entry_store
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://entry"
        ) as entry:
            logged = await entry.post(
                "/api/auth/login", json={"username": "user", "password": "test-only"}
            )
            tokens.append(logged.json()["access_token"])
            result = await entry.post(
                "/api/agents/a/chat/hitl/resume?access_token=do-not-forward",
                json={},
                headers={**bearer(tokens[0]), "X-Tenant-ID": "internal"},
            )
            assert result.status_code == 200
            assert result.content == b'data: {"part": "allowed"}\n\n'
            assert "x-octop-access-token" not in result.headers
            assert "set-cookie" not in result.headers
            assert closed
            assert entry_store.read(tokens[0]) is None
            assert entry_store.read(tokens[0], pending=True).backend_token == "native-renewal"


async def test_upload_aborts_instead_of_committing_after_revocation(entry_store):
    import asyncio

    tokens = []
    received = []
    committed = []

    class Backend(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path == "/api/auth/login":
                return httpx.Response(
                    200, json={"access_token": "native", "expires_in": 3600, "user": {"id": 7}}
                )
            if request.url.path == "/api/auth/me":
                return httpx.Response(200, json={"id": 7})
            async for chunk in request.stream:
                received.append(chunk)
            committed.append(True)
            return httpx.Response(200)

    async with httpx.AsyncClient(transport=Backend(), base_url="http://backend") as backend:
        app = build_candidate({"c": backend}, {"user": Binding("uid", "c", "user", 7)}, entry_store)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://entry"
        ) as entry:
            login_result = await entry.post(
                "/api/auth/login", json={"username": "user", "password": "fake"}
            )
            tokens.append(login_result.json()["access_token"])

            async def upload():
                yield b"first"
                await asyncio.to_thread(entry_store.begin_revoke, tokens[0])
                yield b"after-revoke"

            result = await entry.post("/api/uploads", headers=bearer(tokens[0]), content=upload())
            assert result.status_code == 401
            assert b"".join(received) == b"first"
            assert not committed


@pytest.mark.parametrize("failure", ["database", "cancel"])
async def test_open_response_closed_before_handoff_failure(entry_store, monkeypatch, failure):
    import asyncio

    opened = []
    closed = []
    original = entry_store.read

    def read(*args):
        if opened:
            if failure == "cancel":
                raise asyncio.CancelledError()
            raise RuntimeError("candidate database failure")
        return original(*args)

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"not consumed"

        async def aclose(self):
            closed.append(True)

    def backend(request):
        if request.url.path == "/api/auth/login":
            return httpx.Response(
                200, json={"access_token": "native", "expires_in": 3600, "user": {"id": 7}}
            )
        if request.url.path == "/api/auth/me":
            return httpx.Response(200, json={"id": 7})
        opened.append(True)
        return httpx.Response(200, stream=Stream())

    monkeypatch.setattr(entry_store, "read", read)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(backend), base_url="http://backend"
    ) as client:
        app = build_candidate({"c": client}, {"user": Binding("uid", "c", "user", 7)}, entry_store)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://entry"
        ) as entry:
            logged = await entry.post(
                "/api/auth/login", json={"username": "user", "password": "fake"}
            )
            exception = asyncio.CancelledError if failure == "cancel" else RuntimeError
            with pytest.raises(exception):
                await entry.get("/api/agents", headers=bearer(logged.json()["access_token"]))
            assert closed


async def test_header_disconnect_closes_backend_before_body_starts(entry_store):
    from starlette.requests import ClientDisconnect

    closed = []
    iterated = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            iterated.append(True)
            yield b"never starts"

        async def aclose(self):
            closed.append(True)

    def backend(request):
        if request.url.path == "/api/auth/me":
            return httpx.Response(200, json={"id": 7})
        return httpx.Response(200, stream=Stream())

    entry_store.register("user", Binding("gid", "c", "user", 7))
    token = entry_store.create("user", "native", 3600)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(backend), base_url="http://backend"
    ) as client:
        app = build_candidate({"c": client}, {}, entry_store)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            assert message["type"] == "http.response.start"
            raise OSError("client left before body")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/agents",
            "raw_path": b"/api/agents",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"authorization", ("Bearer " + token).encode())],
            "server": ("entry", 80),
            "client": ("127.0.0.1", 1234),
        }
        with pytest.raises(ClientDisconnect):
            await app(scope, receive, send)
        assert closed
        assert not iterated


async def test_websocket_uses_private_header_and_idle_revocation(env_with_agent, entry_store):
    import asyncio
    import socket
    from contextlib import nullcontext

    import uvicorn
    from starlette.websockets import WebSocketDisconnect

    from tests.support.http import ws_connect

    client, native_server, _auth, agent_id = env_with_agent
    native_server.app_runtime.agent_registry.get_agent(agent_id).chunks = [
        {"type": "token", "node": "agent", "content": "candidate reply"}
    ]
    observed = []

    async def observed_backend(scope, receive, send):
        if scope["type"] == "websocket":
            observed.append((scope.get("query_string"), dict(scope["headers"])))
        await client._octop_app(scope, receive, send)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            observed_backend,
            host="127.0.0.1",
            port=port,
            lifespan="off",
            log_level="error",
            access_log=False,
        )
    )
    server.capture_signals = nullcontext
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:

        async def ready():
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)

        await asyncio.wait_for(ready(), timeout=5)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False
        ) as backend:
            app = build_candidate(
                {"internal": backend},
                {"admin": Binding("global-admin", "internal", "admin", 1)},
                entry_store,
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://entry"
            ) as entry:
                login_response = await entry.post(
                    "/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD}
                )
                token = login_response.json()["access_token"]
                native = entry_store.read(token).backend_token
                async with ws_connect(app, f"/api/agents/{agent_id}/chat/ws?token={token}") as ws:
                    await ws.send_json({"type": "ping"})
                    assert (await ws.receive_json())["type"] == "pong"
                    await ws.send_json({"type": "user_turn", "text": "hello candidate"})
                    frames = await ws.drain_turn()
                    assert frames[-1]["type"] == "done", frames
                    assert any(frame.get("content") == "candidate reply" for frame in frames)

                    assert observed[0][0] == b""
                    assert observed[0][1][b"authorization"] == ("Bearer " + native).encode()
                # Fresh idle connection avoids mistaking pre-revocation buffered frames for new output.
                async with ws_connect(app, f"/api/agents/{agent_id}/chat/ws?token={token}") as ws:
                    await ws.send_json({"type": "ping"})
                    assert (await ws.receive_json())["type"] == "pong"
                    await asyncio.to_thread(entry_store.begin_revoke, token)
                    with pytest.raises(WebSocketDisconnect) as closed:
                        await ws.receive_json(timeout=4)
                    assert closed.value.code == 4001
                logged = await entry.post(
                    "/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD}
                )
                fresh = logged.json()["access_token"]
                async with ws_connect(app, f"/api/agents/{agent_id}/chat/ws?token={fresh}") as ws:
                    await ws.send_json({"type": "ping"})
                    assert (await ws.receive_json())["type"] == "pong"
                    native_fresh = entry_store.read(fresh).backend_token
                    receipt = await backend.post(
                        "/api/auth/session/revoke", headers=bearer(native_fresh)
                    )
                    assert receipt.status_code == 204
                    with pytest.raises(WebSocketDisconnect) as closed:
                        await ws.receive_json(timeout=4)
                    assert closed.value.code == 4001
                logged = await entry.post(
                    "/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD}
                )
                fresh = logged.json()["access_token"]
                async with ws_connect(app, f"/api/agents/{agent_id}/chat/ws?token={fresh}") as ws:
                    await ws.send_json({"type": "ping"})
                    assert (await ws.receive_json())["type"] == "pong"
                    await native_server.user_manager.disable("admin")
                    with pytest.raises(WebSocketDisconnect) as closed:
                        await ws.receive_json(timeout=4)
                    assert closed.value.code == 4001
                await native_server.user_manager.enable("admin")
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        sock.close()


async def test_websocket_redirect_never_forwards_native_credentials(entry_store):
    from http import HTTPStatus

    from starlette.websockets import WebSocketDisconnect
    from websockets.asyncio.server import serve

    from tests.support.http import ws_connect

    received = []

    async def target(connection):
        received.append(dict(connection.request.headers))
        await connection.close()

    async def unused(connection):
        raise AssertionError("Redirected handshake should not reach handler")

    async with serve(target, "127.0.0.1", 0) as destination:
        target_port = destination.sockets[0].getsockname()[1]

        def redirect(connection, request):
            response = connection.respond(HTTPStatus.FOUND, "redirect")
            response.headers["Location"] = f"ws://127.0.0.1:{target_port}/stolen"
            return response

        async with serve(unused, "127.0.0.1", 0, process_request=redirect) as origin:
            port = origin.sockets[0].getsockname()[1]
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False
            ) as backend:
                entry_store.register("user", Binding("uid", "c", "user", 7))
                token = entry_store.create("user", "synthetic-native-secret", 3600)
                app = build_candidate({"c": backend}, {}, entry_store)
                with pytest.raises(WebSocketDisconnect):
                    async with ws_connect(app, f"/api/agents/a/chat/ws?token={token}"):
                        raise AssertionError("Redirect must not be accepted")
                assert not received


async def test_login_does_not_follow_redirect_even_if_client_default_does(entry_store):
    leaked = []

    def backend(request):
        if request.url.host == "backend":
            return httpx.Response(307, headers={"Location": "https://other-origin/capture"})
        leaked.append(request.content)
        return httpx.Response(
            200, json={"access_token": "bad", "expires_in": 3600, "user": {"id": 7}}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(backend), base_url="http://backend", follow_redirects=True
    ) as client:
        app = build_candidate({"c": client}, {"user": Binding("uid", "c", "user", 7)}, entry_store)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://entry"
        ) as entry:
            response = await entry.post(
                "/api/auth/login", json={"username": "user", "password": "test-only"}
            )
            assert response.status_code == 401
            assert not leaked
