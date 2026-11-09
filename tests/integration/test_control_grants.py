"""Signed control routes: one account's role, expert list and limit, idempotent per operation."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from octop.infra.unified_entry.admin import ControlTarget, signed_request
from tests.support.app import octop_client
from tests.support.auth import (
    TEST_PASSWORD,
    auth_header,
    bootstrap_admin,
    create_agent,
    seed_openai_provider,
)

KEY = "c" * 32
TARGET = ControlTarget("internal", KEY)


@pytest.fixture
async def controlled(tmp_octop_home, monkeypatch):
    monkeypatch.setenv("OCTOP_CONTROL_PLANE_INSTANCE", "internal")
    monkeypatch.setenv("OCTOP_CONTROL_PLANE_KEY", KEY)
    async with octop_client(tmp_octop_home) as (client, server):
        await bootstrap_admin(client, tmp_octop_home)
        admin = await auth_header(client)
        await seed_openai_provider(client, admin)
        source = await create_agent(client, admin)
        yield client, server, admin, source


async def _publish(client: httpx.AsyncClient, admin: dict[str, str], source: str, name: str) -> str:
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


def _grants(uid: int) -> str:
    return f"/api/control/users/{uid}/grants"


async def _put(
    client: httpx.AsyncClient,
    uid: int,
    operation_id: str,
    version: int,
    *,
    role: str = "user",
    templates: list[str],
    max_agents: int | None,
) -> httpx.Response:
    payload = {
        "operation_id": operation_id,
        "version": version,
        "role": role,
        "templates": templates,
        "max_agents": max_agents,
    }
    return await signed_request(client, TARGET, "PUT", _grants(uid), payload)


def _error(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 409, response.text
    return dict(response.json()["error"])


async def test_control_routes_do_not_exist_without_a_key(env, monkeypatch):
    client, _server, _admin = env
    response = await signed_request(client, TARGET, "GET", _grants(1))
    assert response.status_code == 404


async def test_signed_apply_reads_back_and_replays_without_reinstalling(controlled):
    client, server, admin, source = controlled
    template = await _publish(client, admin, source, "Managed template")
    role = await client.post(
        "/api/users/roles",
        headers=admin,
        json={
            "user_role_name": "Managed role",
            "permissions": [],
            "policies": [
                {"name": "allowed_expert_templates", "value": json.dumps([template])},
                {"name": "token_quota", "value": "1000"},
            ],
        },
    )
    assert role.status_code in (200, 201), role.text
    role_id = role.json()["user_role_id"]
    uid = await _user(client, admin, "managed")
    services = server.services

    # Only the configured key, for this instance, over this exact request, gets through.
    assert (await client.get(_grants(uid))).status_code == 401
    for target in (ControlTarget("customer", KEY), ControlTarget("internal", "x" * 32)):
        assert (await signed_request(client, target, "GET", _grants(uid))).status_code == 401

    preview_path = f"/api/control/users/{uid}/preview"
    desired = {"role": role_id, "templates": [template]}
    tight = await signed_request(client, TARGET, "POST", preview_path, {**desired, "max_agents": 0})
    assert tight.status_code == 200, tight.text
    assert tight.json()["capacity_ok"] is False
    assert tight.json()["plan"]["install"] == [template]
    roomy = await signed_request(client, TARGET, "POST", preview_path, {**desired, "max_agents": 2})
    assert roomy.json()["capacity_ok"] is True
    assert services.agent_repo.count_by_user(uid) == 0

    applied = await _put(
        client, uid, "op-first-0001", 1, role=role_id, templates=[template], max_agents=2
    )
    assert applied.status_code == 200, applied.text
    receipt = applied.json()
    assert receipt["state"] == "applied" and receipt["replayed"] is False
    assert {name: part["state"] for name, part in receipt["components"].items()} == {
        "role": "applied",
        "max_agents": "applied",
        "experts": "applied",
    }
    readback = (await signed_request(client, TARGET, "GET", _grants(uid))).json()
    assert readback["role"] == role_id and readback["templates"] == [template]
    assert readback["max_agents"] == 2 and readback["pending"] == {}
    assert readback["record"]["operation_id"] == "op-first-0001"
    assert readback["record"]["version"] == 1
    assert services.agent_repo.count_by_user(uid) == 1
    installed = services.agent_repo.list_by_user(uid)[0].agent_id
    await server.app_runtime.agent_registry.assert_user_can_invoke(installed, uid)
    assert services.user_policy_repo.get(uid, "token_quota").value == "1000"

    # Same operation again: the stored receipt, nothing re-run.
    again = await _put(
        client, uid, "op-first-0001", 1, role=role_id, templates=[template], max_agents=2
    )
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert services.agent_repo.count_by_user(uid) == 1

    # Same ID with other content, or an older version under a new ID: refused, nothing changes.
    mismatch = await _put(
        client, uid, "op-first-0001", 1, role=role_id, templates=[template], max_agents=3
    )
    assert _error(mismatch)["code"] == "CONTROL_OPERATION_CONFLICT"
    assert _error(mismatch)["details"]["reason"] == "operation_mismatch"
    stale = await _put(client, uid, "op-older-0001", 1, templates=[], max_agents=None)
    assert _error(stale)["details"] == {"reason": "stale_version", "applied_version": 1}
    assert services.user_repo.get(uid).role == role_id

    # The instance's own admin routes no longer write these three for this account.
    for body in ({"role": "user"}, {"max_agents": 5}):
        blocked = await client.patch(f"/api/users/{uid}", headers=admin, json=body)
        assert _error(blocked)["code"] == "ACCOUNT_CENTRALLY_MANAGED"
    batch = await client.post(
        "/api/users/batch",
        headers=admin,
        json={"user_ids": [uid], "action": "set_max_agents", "max_agents": 5},
    )
    assert batch.status_code == 200, batch.text
    assert batch.json()["results"][0]["code"] == "ACCOUNT_CENTRALLY_MANAGED"
    assignment = f"/api/users/{uid}/expert-assignment"
    plan = (
        await client.post(assignment + "/preview", headers=admin, json={"templates": []})
    ).json()
    native = await client.put(
        assignment, headers=admin, json={"templates": [], "revision": plan["revision"]}
    )
    assert _error(native)["code"] == "ACCOUNT_CENTRALLY_MANAGED"
    # The same rule holds inside the grant transaction, behind the route checks.
    from octop.infra.errors import OctopError
    from octop.infra.users.expert_access import INSTALLING_EXPERT_PREFIX, POLICY_ASSIGNED_EXPERTS
    from octop.infra.users.expert_assignment import ControlActor, _record_change
    from octop.infra.users.resource_policy import POLICY_MAX_AGENTS

    admin_id = services.user_repo.get_by_username("admin").id
    with pytest.raises(OctopError) as managed:
        _record_change(services, admin_id, uid, {POLICY_ASSIGNED_EXPERTS: "[]"}, "test", None)
    assert managed.value.code.value == "ACCOUNT_CENTRALLY_MANAGED"
    # Clearing an install reservation grants nothing and stays allowed.
    _record_change(services, admin_id, uid, {INSTALLING_EXPERT_PREFIX + "x": None}, "test", None)
    # Fields outside the three stay editable natively.
    quota = await client.patch(f"/api/users/{uid}", headers=admin, json={"token_quota": 5})
    assert quota.status_code == 200, quota.text
    after = (await signed_request(client, TARGET, "GET", _grants(uid))).json()
    assert (after["role"], after["max_agents"], after["templates"]) == (role_id, 2, [template])

    # A newer operation that keeps the role does not re-apply the role template.
    newer = await _put(
        client, uid, "op-second-001", 2, role=role_id, templates=[template], max_agents=3
    )
    assert newer.json()["state"] == "applied", newer.text
    assert newer.json()["readback"]["max_agents"] == 3
    assert services.user_policy_repo.get(uid, "token_quota").value == "5"
    assert services.agent_repo.count_by_user(uid) == 1
    # Writes of a superseded operation are fenced off.
    with pytest.raises(OctopError) as fenced:
        _record_change(
            services,
            ControlActor("control-plane:internal", "op-first-0001"),
            uid,
            {POLICY_MAX_AGENTS: "9"},
            "test",
            None,
        )
    assert fenced.value.details == {"reason": "superseded"}
    assert services.user_policy_repo.get(uid, POLICY_MAX_AGENTS).value == "3"
    claimed = services.audit_repo.query(action="user.control_operation_claimed")
    assert {(row.actor, row.target) for row in claimed} == {("control-plane:internal", str(uid))}

    # A limit below the experts the account keeps fails the experts step, even when every
    # expert is reused, matching the preview's capacity_ok.
    over = await _put(
        client, uid, "op-third-001", 3, role=role_id, templates=[template], max_agents=0
    )
    assert over.json()["state"] == "partial", over.text
    assert over.json()["components"]["experts"] == {
        "state": "failed",
        "code": "AGENT_QUOTA_EXCEEDED",
        "details": {"required": 1, "limit": 0},
    }
    assert services.agent_repo.count_by_user(uid) == 1


async def test_same_operation_claimed_by_another_process_is_resumed_not_refused(
    controlled, monkeypatch
):
    from dataclasses import replace

    from octop.infra.users import control_grants
    from octop.infra.users.control_grants import ComponentResult, _settle, read_grants

    client, server, admin, source = controlled
    template = await _publish(client, admin, source, "Raced template")
    uid = await _user(client, admin, "raced")
    services = server.services
    real_claim = control_grants._claim

    def claimed_elsewhere_first(*args: Any) -> None:
        real_claim(*args)  # another process delivering the same operation wins the claim
        real_claim(*args)  # this one now loses the compare-and-set

    monkeypatch.setattr(control_grants, "_claim", claimed_elsewhere_first)
    raced = await _put(client, uid, "op-raced-0001", 1, templates=[template], max_agents=1)
    assert raced.status_code == 200, raced.text
    assert raced.json()["state"] == "applied" and raced.json()["replayed"] is False
    assert services.agent_repo.count_by_user(uid) == 1

    # A late failed outcome of the same operation never replaces the applied one.
    stored = read_grants(services, uid).record
    late = replace(
        stored,
        state="partial",
        components={**stored.components, "experts": ComponentResult("failed")},
    )
    assert _settle(services, uid, late, "control-plane:internal") == stored
    assert read_grants(services, uid).record == stored


async def test_failed_install_is_partial_and_the_same_operation_resumes_once(
    controlled, monkeypatch
):
    from octop.infra.agents.experts import default_agent
    from octop.infra.errors import ErrorCode, OctopError

    client, server, admin, source = controlled
    first = await _publish(client, admin, source, "First template")
    other = await create_agent(client, admin, name="second-source")
    second = await _publish(client, admin, other, "Second template")
    uid = await _user(client, admin, "resumed")
    services = server.services
    original = default_agent.install_role_published_expert
    calls: list[str] = []

    async def fail_second_install(server: Any, **kwargs: Any) -> Any:
        calls.append(kwargs["expert_id"])
        if len(calls) == 2:
            raise OctopError(ErrorCode.EXPERT_ASSIGNMENT_CONFLICT, "install failed", status=409)
        return await original(server, **kwargs)

    monkeypatch.setattr(default_agent, "install_role_published_expert", fail_second_install)
    desired: dict[str, Any] = {"templates": [first, second], "max_agents": 2}
    partial = await _put(client, uid, "op-resume-001", 1, **desired)
    assert partial.status_code == 200, partial.text
    receipt = partial.json()
    assert receipt["state"] == "partial"
    assert receipt["components"]["max_agents"]["state"] == "applied"
    assert receipt["components"]["experts"] == {
        "state": "failed",
        "code": "EXPERT_ASSIGNMENT_CONFLICT",
        "details": {},
    }
    assert receipt["readback"]["templates"] is None
    assert receipt["readback"]["record"]["state"] == "partial"
    assert services.agent_repo.count_by_user(uid) == 1

    resumed = await _put(client, uid, "op-resume-001", 1, **desired)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "applied" and resumed.json()["replayed"] is False
    assert resumed.json()["readback"]["templates"] == [first, second]
    assert calls == [first, second, second]
    assert services.agent_repo.count_by_user(uid) == 2
    replay = await _put(client, uid, "op-resume-001", 1, **desired)
    assert replay.json()["replayed"] is True
    assert calls == [first, second, second]
    assert services.agent_repo.count_by_user(uid) == 2


def test_only_grant_writes_are_blocked_for_managed_accounts():
    from octop.infra.users.expert_access import INSTALLING_EXPERT_PREFIX, POLICY_ASSIGNED_EXPERTS
    from octop.infra.users.expert_assignment import _grants_write
    from octop.infra.users.resource_policy import POLICY_MAX_AGENTS

    key = INSTALLING_EXPERT_PREFIX + "template"
    assert _grants_write({POLICY_ASSIGNED_EXPERTS: "[]"})
    assert _grants_write({POLICY_MAX_AGENTS: None})
    assert _grants_write({key: "fresh-reservation-id"})
    # Discarding or verifying an install records an outcome and grants nothing.
    assert not _grants_write({key: None})
    assert not _grants_write({key: "discarding:fresh-reservation-id"})
    assert not _grants_write({key: "ready:fresh-reservation-id"})


async def test_late_delivery_of_a_finished_operation_keeps_later_quota_edits(
    controlled, monkeypatch
):
    """Two processes deliver one role change; the late one must not reset the role's quota."""
    import asyncio
    from dataclasses import replace

    from octop.infra.users.control_grants import (
        COMPONENTS,
        ComponentResult,
        ControlGrants,
        ControlRecord,
        DesiredGrants,
        _claim,
        _settle,
    )
    from octop.infra.users.expert_assignment import ExpertAssignments

    client, server, admin, _source = controlled
    role = await client.post(
        "/api/users/roles",
        headers=admin,
        json={
            "user_role_name": "Quota role",
            "permissions": [],
            "policies": [{"name": "token_quota", "value": "1000"}],
        },
    )
    assert role.status_code in (200, 201), role.text
    uid = await _user(client, admin, "twice")
    services = server.services
    real_set_role = server.user_manager.set_role
    entered, release = asyncio.Event(), asyncio.Event()

    async def first_waits(username: str, role_id: str) -> None:
        if not entered.is_set():
            entered.set()  # the slow delivery has passed its checks and is about to write
            await release.wait()
        await real_set_role(username, role_id)

    monkeypatch.setattr(server.user_manager, "set_role", first_waits)
    desired = DesiredGrants(role=role.json()["user_role_id"], templates=[], max_agents=None)

    def deliver() -> Any:
        # A separate ControlGrants has its own account locks, like another process.
        grants = ControlGrants(ExpertAssignments())
        return grants.apply(server, uid, "op-twice-0001", 1, desired, "internal")

    slow = asyncio.create_task(deliver())
    await asyncio.wait_for(entered.wait(), 10)
    fast = await deliver()
    assert fast.state == "applied" and fast.replayed is False
    assert services.user_policy_repo.get(uid, "token_quota").value == "1000"
    quota = await client.patch(f"/api/users/{uid}", headers=admin, json={"token_quota": 5})
    assert quota.status_code == 200, quota.text
    release.set()
    late = await asyncio.wait_for(slow, 10)
    assert late.state == "applied"
    assert services.user_policy_repo.get(uid, "token_quota").value == "5"

    # A late outcome never marks undone a component another delivery finished.
    other = await _user(client, admin, "merged")
    record = ControlRecord(
        operation_id="op-merge-0001",
        version=1,
        digest="d" * 64,
        state="applying",
        components={
            "role": ComponentResult("applied"),
            "max_agents": ComponentResult("pending"),
            "experts": ComponentResult("pending"),
        },
        role_change=True,
        updated_at=0,
    )
    _claim(services, other, None, record, "control-plane:internal")
    failed = replace(
        record,
        state="partial",
        components={name: ComponentResult("failed") for name in COMPONENTS},
    )
    stored = _settle(services, other, failed, "control-plane:internal")
    assert stored.state == "partial"
    assert stored.components["role"].state == "applied"
    assert stored.components["experts"].state == "failed"
