"""Unified admin entry over two real instances: preview, apply, readback, retry, idempotency."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from typing import Any

import httpx
import pytest
from prototypes.unified_entry.app import Binding, build_candidate

from octop.infra.unified_entry.admin import (
    ControlTarget,
    reconcile_admin_operations,
    signed_request,
)
from tests.integration import test_unified_entry_candidate as candidate
from tests.support.app import octop_client, write_octop_config
from tests.support.auth import (
    TEST_PASSWORD,
    auth_header,
    bearer,
    bootstrap_admin,
    create_agent,
    seed_openai_provider,
)

KEYS = {"internal": "i" * 32, "customer": "u" * 32}
# Same isolated entry database as the login and proxy tests.
entry_store_factory = candidate.entry_store_factory
entry_store = candidate.entry_store


class Switch(httpx.AsyncBaseTransport):
    """An instance link that can go down, lose the reply to a delivered PUT, fail or serve an
    outdated control readback."""

    def __init__(self, app: Any) -> None:
        self.inner = httpx.ASGITransport(app=app)
        self.mode = "up"

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        readback = request.method == "GET" and request.url.path.startswith("/api/control/")
        if self.mode == "down" or (self.mode == "readback_down" and readback):
            raise httpx.ConnectError("instance down", request=request)
        response = await self.inner.handle_async_request(request)
        if self.mode == "lose_reply" and request.method == "PUT":
            await response.aread()
            raise httpx.ReadError("reply lost", request=request)
        if self.mode == "old_readback" and request.method == "GET":
            data = json.loads(await response.aread())
            data["max_agents"] -= 1
            return httpx.Response(response.status_code, json=data, request=request)
        return response


async def _publish(client: httpx.AsyncClient, admin: dict[str, str], name: str) -> str:
    source = await create_agent(client, admin, name=name + " source")
    published = await client.post(
        f"/api/agents/{source}/publish-expert", headers=admin, json={"name": name}
    )
    assert published.status_code == 201, published.text
    return str(published.json()["id"])


async def _user(client: httpx.AsyncClient, admin: dict[str, str], username: str) -> int:
    created = await client.post(
        "/api/users",
        headers=admin,
        json={"username": username, "password": TEST_PASSWORD, "role": "user"},
    )
    assert created.status_code == 201, created.text
    return int(created.json()["id"])


async def test_entry_applies_grants_once_and_confirms_them_by_readback(
    tmp_path, entry_store, monkeypatch
):
    from octop.infra.agents.experts import default_agent
    from octop.infra.errors import ErrorCode, OctopError

    monkeypatch.delenv("OCTOP_CONTROL_PLANE_INSTANCE", raising=False)
    monkeypatch.delenv("OCTOP_CONTROL_PLANE_KEY", raising=False)
    async with AsyncExitStack() as stack:
        raw, servers, links, templates, users = {}, {}, {}, {}, {}
        bindings: dict[str, Binding] = {}
        for name in ("internal", "customer"):
            home = tmp_path / name
            home.mkdir()
            write_octop_config(home, control_plane_instance=name, control_plane_key=KEYS[name])
            client, server = await stack.enter_async_context(octop_client(home))
            await bootstrap_admin(client, home)
            admin = await auth_header(client)
            await seed_openai_provider(client, admin)
            templates[name] = [await _publish(client, admin, f"{name} {n}") for n in (1, 2)]
            users[name] = await _user(client, admin, "same_name")
            bindings[name + "/same_name"] = Binding(
                "identity-" + name, name, "same_name", users[name]
            )
            links[name] = Switch(client._octop_app)  # type: ignore[attr-defined]
            raw[name], servers[name] = client, server
        ops_uid = await _user(raw["internal"], await auth_header(raw["internal"]), "ops")
        bindings["internal/ops"] = Binding("identity-ops", "internal", "ops", ops_uid)
        clients = {
            name: await stack.enter_async_context(
                httpx.AsyncClient(transport=link, base_url="http://testserver")
            )
            for name, link in links.items()
        }
        targets = {name: ControlTarget(name, KEYS[name]) for name in KEYS}
        app = build_candidate(clients, bindings, entry_store, control=targets)
        entry = await stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://candidate")
        )

        async def session(alias: str) -> dict[str, str]:
            logged = await entry.post(
                "/api/auth/login", json={"username": alias, "password": TEST_PASSWORD}
            )
            assert logged.status_code == 200, logged.text
            return bearer(logged.json()["access_token"])

        def count(name: str) -> int:
            return int(servers[name].services.agent_repo.count_by_user(users[name]))

        def record(name: str) -> Any:
            return servers[name].services.user_policy_repo.get(users[name], "control_plane_record")

        # Platform admin is an entry flag, not an instance role.
        member = await session("customer/same_name")
        assert (await entry.get("/api/admin/accounts", headers=member)).status_code == 403
        ops = await session("internal/ops")
        assert (await entry.get("/api/admin/accounts", headers=ops)).status_code == 403
        entry_store.grant_platform_admin("identity-ops")
        listed = await entry.get("/api/admin/accounts", headers=ops)
        assert listed.status_code == 200, listed.text
        assert {row["account"]: row["manageable"] for row in listed.json()} == {
            "customer/same_name": True,
            "internal/ops": True,
            "internal/same_name": True,
        }
        assert all(row["latest_operation"] is None for row in listed.json())

        c1, c2 = templates["customer"]
        account = {"account": "customer/same_name", "role": "user"}
        preview = await entry.post(
            "/api/admin/accounts/preview",
            headers=ops,
            json={**account, "templates": [c1, c2], "max_agents": 1},
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["capacity_ok"] is False
        assert preview.json()["plan"]["install"] == [c1, c2]

        # Applied only after the instance's receipt and a matching readback.
        first = {**account, "templates": [c1], "max_agents": 2, "operation_id": "entry-op-0001"}
        applied = await entry.put("/api/admin/accounts/grants", headers=ops, json=first)
        assert applied.status_code == 200, applied.text
        assert applied.json()["state"] == "applied" and applied.json()["version"] == 1
        assert applied.json()["readback"]["templates"] == [c1]
        assert count("customer") == 1 and record("internal") is None

        # Resubmitting the same operation is the same change: no new version, no new install.
        again = await entry.put("/api/admin/accounts/grants", headers=ops, json=first)
        assert again.json() == applied.json()
        assert entry_store.operations.latest_for_account("customer/same_name").version == 1
        assert count("customer") == 1
        reused = await entry.put(
            "/api/admin/accounts/grants", headers=ops, json={**first, "max_agents": 3}
        )
        assert reused.status_code == 409

        # The instance applied it but the reply was lost: pending, then confirmed by replay.
        links["customer"].mode = "lose_reply"
        both = {**account, "templates": [c1, c2], "max_agents": 2}
        lost = await entry.put("/api/admin/accounts/grants", headers=ops, json=both)
        assert lost.status_code == 200, lost.text
        lost_op = lost.json()
        assert lost_op["state"] == "pending" and lost_op["version"] == 2
        assert count("customer") == 2
        links["customer"].mode = "up"
        assert (
            await reconcile_admin_operations(entry_store.operations, clients, targets)
        ).applied == 1
        confirmed = await entry.get(f"/api/admin/operations/{lost_op['operation_id']}", headers=ops)
        assert confirmed.json()["state"] == "applied"
        assert confirmed.json()["result"]["replayed"] is True
        assert count("customer") == 2

        # Instance down: stays pending across passes, applied once it is back.
        links["customer"].mode = "down"
        wider = await entry.put(
            "/api/admin/accounts/grants",
            headers=ops,
            json={**account, "templates": [c1, c2], "max_agents": 3},
        )
        assert wider.json()["state"] == "pending"
        assert wider.json()["result"] == {"error": "instance unreachable"}
        assert (
            await reconcile_admin_operations(entry_store.operations, clients, targets)
        ).applied == 0
        links["customer"].mode = "up"
        assert (
            await reconcile_admin_operations(entry_store.operations, clients, targets)
        ).applied == 1
        wider_op = await entry.get(
            f"/api/admin/operations/{wider.json()['operation_id']}", headers=ops
        )
        assert wider_op.json()["state"] == "applied"
        assert wider_op.json()["readback"]["max_agents"] == 3
        assert count("customer") == 2

        # An applied receipt whose readback disagrees is not trusted: pending until it matches.
        links["customer"].mode = "old_readback"
        unseen = await entry.put(
            "/api/admin/accounts/grants",
            headers=ops,
            json={**account, "templates": [c1, c2], "max_agents": 4},
        )
        assert unseen.json()["state"] == "pending"
        assert unseen.json()["result"]["state"] == "applied"
        assert unseen.json()["readback"]["max_agents"] == 3
        links["customer"].mode = "up"
        assert (
            await reconcile_admin_operations(entry_store.operations, clients, targets)
        ).applied == 1
        seen = await entry.get(
            f"/api/admin/operations/{unseen.json()['operation_id']}", headers=ops
        )
        assert seen.json()["state"] == "applied" and seen.json()["readback"]["max_agents"] == 4

        # A component failed: partial; an admin retry resumes the same operation.
        i1, i2 = templates["internal"]
        original = default_agent.install_role_published_expert
        calls: list[str] = []

        async def fail_second_install(server: Any, **kwargs: Any) -> Any:
            calls.append(kwargs["expert_id"])
            if len(calls) == 2:
                raise OctopError(ErrorCode.EXPERT_ASSIGNMENT_CONFLICT, "install failed", status=409)
            return await original(server, **kwargs)

        monkeypatch.setattr(default_agent, "install_role_published_expert", fail_second_install)
        internal = {
            "account": "internal/same_name",
            "role": "user",
            "templates": [i1, i2],
            "max_agents": None,
        }
        # The readback fails too: the partial receipt alone settles it, nothing re-applies it.
        links["internal"].mode = "readback_down"
        partial = await entry.put("/api/admin/accounts/grants", headers=ops, json=internal)
        assert partial.json()["state"] == "partial", partial.text
        assert partial.json()["result"]["components"]["experts"]["state"] == "failed"
        assert partial.json()["readback"] is None
        assert count("internal") == 1
        links["internal"].mode = "up"
        assert (
            await reconcile_admin_operations(entry_store.operations, clients, targets)
        ).applied == 0
        assert calls == [i1, i2]
        retry_path = f"/api/admin/operations/{partial.json()['operation_id']}/retry"
        retried = await entry.post(retry_path, headers=ops)
        assert retried.status_code == 200, retried.text
        assert retried.json()["state"] == "applied"
        assert retried.json()["readback"]["templates"] == [i1, i2]
        assert calls == [i1, i2, i2] and count("internal") == 2
        assert (await entry.post(retry_path, headers=ops)).json()["state"] == "applied"
        assert calls == [i1, i2, i2]

        # Refused by the instance: rejected, never retried.
        bad = await entry.put(
            "/api/admin/accounts/grants",
            headers=ops,
            json={**account, "role": "no-such-role", "templates": [], "max_agents": 0},
        )
        assert bad.json()["state"] == "rejected" and bad.json()["result"]["status"] == 404
        bad_retry = f"/api/admin/operations/{bad.json()['operation_id']}/retry"
        assert (await entry.post(bad_retry, headers=ops)).status_code == 409
        assert record("customer") is not None
        readback = await signed_request(
            raw["customer"],
            targets["customer"],
            "GET",
            f"/api/control/users/{users['customer']}/grants",
        )
        assert readback.json()["record"]["operation_id"] == unseen.json()["operation_id"]

        # A call signed for one instance is not accepted by the other.
        for target in (targets["internal"], ControlTarget("customer", KEYS["internal"])):
            crossed = await signed_request(
                raw["customer"], target, "GET", f"/api/control/users/{users['customer']}/grants"
            )
            assert crossed.status_code == 401
        assert (
            await entry.post(
                "/api/admin/accounts/preview",
                headers=ops,
                json={**account, "account": "nobody/none", "templates": [], "max_agents": 0},
            )
        ).status_code == 404


@pytest.mark.parametrize("control", [None, {}])
async def test_admin_routes_are_off_without_instance_keys(tmp_path, entry_store, control):
    async with octop_client(tmp_path / "solo") as (client, _server):
        await bootstrap_admin(client, tmp_path / "solo")
        app = build_candidate({"solo": client}, {}, entry_store, control=control)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://candidate"
        ) as entry:
            assert (await entry.get("/api/admin/accounts")).status_code == 404


async def test_retry_passes_reach_every_pending_operation(entry_store):
    """Operations that keep failing, or whose instance is gone, never starve the rest."""
    repo = entry_store.operations
    seen = {"down": 0, "up": 0}

    def answer(name: str) -> Any:
        def handle(request: httpx.Request) -> httpx.Response:
            seen[name] += 1
            if name == "down":
                raise httpx.ConnectError("instance down", request=request)
            return httpx.Response(503, request=request)

        return handle

    repo.create_next("gone-op-0000", "gone/a", "gone", 1, "{}")
    for n in range(3):
        repo.create_next(f"down-op-000{n}", "down/a", "down", 1, "{}")
    repo.create_next("up-op-00000", "up/a", "up", 1, "{}")
    targets = {name: ControlTarget(name, "k" * 32) for name in seen}
    async with AsyncExitStack() as stack:
        clients = {
            name: await stack.enter_async_context(
                httpx.AsyncClient(transport=httpx.MockTransport(answer(name)), base_url="http://x")
            )
            for name in seen
        }
        cursor = 0
        for _ in range(2):
            done = await reconcile_admin_operations(repo, clients, targets, cursor, limit=2)
            cursor = done.cursor
    assert seen == {"down": 3, "up": 1}
    assert repo.get("gone-op-0000").state == "pending"


async def test_late_or_stale_deliveries_never_reopen_a_partial_operation(entry_store):
    """A partial operation only goes back to pending through an admin retry."""
    import asyncio

    from octop.infra.unified_entry.admin import AdminOperation, dispatch

    repo = entry_store.operations
    target = ControlTarget("internal", "k" * 32)
    partial_receipt = {"state": "partial", "components": {}}
    puts: list[str] = []
    slow_sent, slow_go = asyncio.Event(), asyncio.Event()

    async def late_timeout(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"record": None}, request=request)
        puts.append(request.url.path)
        if len(puts) == 1:
            slow_sent.set()
            await slow_go.wait()
            raise httpx.ReadTimeout("slow reply", request=request)
        return httpx.Response(200, json=partial_receipt, request=request)

    row = repo.create_next("late-op-0001", "internal/a", "internal", 1, "{}")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(late_timeout), base_url="http://x"
    ) as client:
        slow = asyncio.create_task(dispatch(repo, AdminOperation.from_row(row), client, target))
        await asyncio.wait_for(slow_sent.wait(), 10)
        fast = await dispatch(repo, AdminOperation.from_row(row), client, target)
        assert fast.state == "partial"
        slow_go.set()
        await slow
        assert repo.get("late-op-0001").state == "partial"
        assert json.loads(repo.get("late-op-0001").result) == partial_receipt
        done = await reconcile_admin_operations(repo, {"internal": client}, {"internal": target})
        assert done.applied == 0 and len(puts) == 2

    # A pass's batch is read once; a row settled by an admin request meanwhile is skipped.
    first = repo.create_next("batch-op-0001", "internal/b", "internal", 2, "{}")
    repo.create_next("batch-op-0002", "internal/c", "internal", 3, "{}")
    sent: list[str] = []

    def settle_the_second(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        repo.record("batch-op-0002", "partial", json.dumps(partial_receipt), None)
        return httpx.Response(503, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(settle_the_second), base_url="http://x"
    ) as client:
        await reconcile_admin_operations(repo, {"internal": client}, {"internal": target})
    assert sent == [f"/api/control/users/{first.user_id}/grants"]
    assert repo.get("batch-op-0002").state == "partial"
