"""A second assigned expert must retain the user's customized first instance."""

from typing import Any

from tests.support.auth import TEST_PASSWORD, bearer, login


async def test_preview_preserves_existing_copy_and_reports_capacity(env_with_agent):
    client, server, admin, source = env_with_agent
    published = await client.post(
        f"/api/agents/{source}/publish-expert", headers=admin, json={"name": "Second template"}
    )
    assert published.status_code == 201, published.text
    second = published.json()["id"]
    user = await client.post(
        "/api/users",
        headers=admin,
        json={
            "username": "preview-user",
            "password": TEST_PASSWORD,
            "role": "user",
            "max_agents": 1,
        },
    )
    assert user.status_code == 201, user.text
    uid = user.json()["id"]
    server.services.agent_repo.create(
        agent_id="retained",
        user_id=uid,
        name="My custom name",
        system_prompt="My custom prompt",
        published_expert_id="first-template",
    )
    server.services.thread_repo.insert(
        thread_id="retained-thread",
        agent_id="retained",
        user_id=uid,
        channel_type="dashboard",
        session_key="retained-key",
    )
    path = f"/api/users/{uid}/expert-assignment/preview"
    desired = {"templates": ["first-template", second, second]}
    auth = bearer(await login(client, username="preview-user"))
    assert (await client.post(path, headers=auth, json=desired)).status_code == 403
    result = await client.post(path, headers=admin, json=desired)
    assert result.status_code == 200, result.text
    plan = result.json()
    assert plan["reuse"] == {"first-template": ["retained"]}
    assert plan["install"] == [second] and plan["conflicts"] == {}
    assert (
        plan["current_limit"] == 1
        and plan["required_capacity"] == 2
        and plan["needs_capacity_increase"]
    )
    assert (await client.post(path, headers=admin, json=desired)).json() == plan
    assert server.services.agent_repo.get("retained").system_prompt == "My custom prompt"
    assert server.services.thread_repo.get("retained-thread").user_id == uid
    assert server.services.agent_repo.count_by_user(uid) == 1
    apply_path = f"/api/users/{uid}/expert-assignment"
    payload = {**desired, "revision": plan["revision"]}
    assert (await client.put(apply_path, headers=auth, json=payload)).status_code == 403
    assert (await client.put(apply_path, headers=admin, json=payload)).status_code == 409
    payload["raise_limit_to"] = 2
    applied = await client.put(apply_path, headers=admin, json=payload)
    assert applied.status_code == 200, applied.text
    receipt = applied.json()
    assert receipt["install"] == [] and receipt["current_limit"] == 2
    assert receipt["reuse"]["first-template"] == ["retained"]
    assert len(receipt["reuse"][second]) == 1
    assert (await client.put(apply_path, headers=admin, json=payload)).json() == receipt
    assert server.services.agent_repo.count_by_user(uid) == 2
    assert server.services.agent_repo.get("retained").system_prompt == "My custom prompt"
    assert server.services.thread_repo.get("retained-thread").user_id == uid
    registry = server.app_runtime.agent_registry
    await registry.assert_user_can_invoke(receipt["reuse"][second][0], uid)
    revoked = await client.post(path, headers=admin, json={"templates": ["first-template"]})
    removed = await client.put(
        apply_path,
        headers=admin,
        json={"templates": ["first-template"], "revision": revoked.json()["revision"]},
    )
    assert removed.status_code == 200, removed.text
    import pytest

    from octop.infra.errors import OctopError

    with pytest.raises(OctopError):
        await registry.assert_user_can_invoke(receipt["reuse"][second][0], uid)
    assert server.services.agent_repo.count_by_user(uid) == 2


async def test_existing_copy_reassignment_audit_failure_preserves_grants(env, monkeypatch):
    import pytest

    from octop.infra.users.expert_access import POLICY_ASSIGNED_EXPERTS, account_expert_grants

    client, server, admin = env
    user = await client.post(
        "/api/users",
        headers=admin,
        json={"username": "swap-user", "password": TEST_PASSWORD, "role": "user", "max_agents": 1},
    )
    assert user.status_code == 201
    uid = user.json()["id"]
    services = server.services
    for template in ["old", "next"]:
        services.agent_repo.create(
            agent_id=template, user_id=uid, name=template, published_expert_id=template
        )
    services.user_policy_repo.set(uid, POLICY_ASSIGNED_EXPERTS, '["old"]')
    path = f"/api/users/{uid}/expert-assignment"
    desired = {"templates": ["next"]}
    preview = await client.post(path + "/preview", headers=admin, json=desired)
    plan = preview.json()
    assert plan["required_capacity"] == 2 and plan["current_limit"] == 1
    payload = {**desired, "revision": plan["revision"]}
    original = services.audit_repo.write_in_transaction

    def fail_final_receipt(conn, **kwargs):
        if kwargs["action"] == "user.assign_experts":
            raise RuntimeError("audit unavailable")
        return original(conn, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(services.audit_repo, "write_in_transaction", fail_final_receipt)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await client.put(path, headers=admin, json=payload)
    assert account_expert_grants(services.repos, uid) == {"old"}
    retry = await client.put(path, headers=admin, json=payload)
    assert retry.status_code == 200, retry.text
    assert account_expert_grants(services.repos, uid) == {"next"}
    assert retry.json()["current_limit"] == 1 and services.agent_repo.count_by_user(uid) == 2
    receipts = services.audit_repo.query(action="user.assign_experts")
    assert [row.target for row in receipts] == [str(uid)]


async def test_incomplete_workspace_stays_blocked_after_other_assignment(
    env_with_agent, monkeypatch
):
    import pytest

    from octop.infra.agents.experts import default_agent
    from octop.infra.errors import OctopError
    from octop.infra.users.expert_access import assert_expert_invocation

    client, server, admin, source = env_with_agent
    published = await client.post(
        f"/api/agents/{source}/publish-expert", headers=admin, json={"name": "Incomplete"}
    )
    template = published.json()["id"]
    user = await client.post(
        "/api/users",
        headers=admin,
        json={
            "username": "partial-user",
            "password": TEST_PASSWORD,
            "role": "user",
            "max_agents": 2,
        },
    )
    uid = user.json()["id"]
    services = server.services
    services.agent_repo.create(
        agent_id="original", user_id=uid, name="Original", published_expert_id="original-template"
    )
    path = f"/api/users/{uid}/expert-assignment"
    desired = {"templates": ["original-template", template]}
    preview = await client.post(path + "/preview", headers=admin, json=desired)

    async def interrupt_after_insert(
        server, *, agent_id, user_id, expert_id, prepared, assignment_limit, reservation
    ):
        server.services.agent_repo.create(
            agent_id=agent_id, user_id=user_id, name="Incomplete", published_expert_id=expert_id
        )
        raise RuntimeError("workspace interrupted")

    monkeypatch.setattr(default_agent, "install_role_published_expert", interrupt_after_insert)
    with pytest.raises(RuntimeError, match="workspace interrupted"):
        await client.put(
            path, headers=admin, json={**desired, "revision": preview.json()["revision"]}
        )

    old = {"templates": ["original-template"]}
    old_preview = await client.post(path + "/preview", headers=admin, json=old)
    kept = await client.put(
        path, headers=admin, json={**old, "revision": old_preview.json()["revision"]}
    )
    assert kept.status_code == 200
    incomplete = await client.post(path + "/preview", headers=admin, json=desired)
    assert incomplete.json()["conflicts"] == {template: "installation_unconfirmed"}
    rejected = await client.put(
        path, headers=admin, json={**desired, "revision": incomplete.json()["revision"]}
    )
    assert rejected.status_code == 409
    partial = next(
        row for row in services.agent_repo.list_by_user(uid) if row.published_expert_id == template
    )
    with pytest.raises(OctopError):
        assert_expert_invocation(services.repos, partial.agent_id, uid)
    assert_expert_invocation(services.repos, "original", uid)


async def test_changed_template_bytes_after_apply_preflight_are_rejected(
    env_with_agent, monkeypatch
):
    from octop.infra.users import expert_assignment

    client, server, admin, source = env_with_agent
    published = await client.post(
        f"/api/agents/{source}/publish-expert", headers=admin, json={"name": "Mutable template"}
    )
    template = published.json()["id"]
    user = await client.post(
        "/api/users",
        headers=admin,
        json={"username": "snapshot-user", "password": TEST_PASSWORD, "role": "user"},
    )
    uid = user.json()["id"]
    path = f"/api/users/{uid}/expert-assignment"
    desired = {"templates": [template]}
    preview = await client.post(path + "/preview", headers=admin, json=desired)
    stage = expert_assignment._stage

    def change_files_before_copy(services, template, directory, version):
        # The database row and its timestamp stay identical.
        (services.paths.published_experts_dir / template / "changed.txt").write_text("changed")
        return stage(services, template, directory, version)

    monkeypatch.setattr(expert_assignment, "_stage", change_files_before_copy)
    rejected = await client.put(
        path, headers=admin, json={**desired, "revision": preview.json()["revision"]}
    )
    assert rejected.status_code == 409, rejected.text
    assert server.services.agent_repo.count_by_user(uid) == 0
    assert server.services.audit_repo.query(action="user.assign_experts") == []


async def test_disabled_operator_cannot_finish_queued_assignment(env, monkeypatch):
    import asyncio

    from octop.infra.users import expert_assignment

    client, server, admin = env
    operator = await client.post(
        "/api/users",
        headers=admin,
        json={
            "username": "operator",
            "password": TEST_PASSWORD,
            "role": "admin",
        },
    )
    assert operator.status_code == 201, operator.text
    auth = bearer(await login(client, username="operator"))
    customer = await client.post(
        "/api/users",
        headers=admin,
        json={
            "username": "queued-user",
            "password": TEST_PASSWORD,
            "role": "user",
        },
    )
    uid = customer.json()["id"]
    path = f"/api/users/{uid}/expert-assignment"
    desired = {"templates": []}
    preview = await client.post(path + "/preview", headers=auth, json=desired)
    started = asyncio.Event()
    original = expert_assignment.ExpertAssignments.apply

    async def signal_before_lock(self, *args, **kwargs):
        started.set()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(expert_assignment.ExpertAssignments, "apply", signal_before_lock)
    lock = asyncio.Lock()
    server.expert_assignments._locks[uid] = lock
    async with lock:
        task = asyncio.create_task(
            client.put(path, headers=auth, json={**desired, "revision": preview.json()["revision"]})
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        await server.user_manager.disable("operator")
    rejected = await task
    assert rejected.status_code == 403, rejected.text
    assert server.services.user_policy_repo.get(uid, "assigned_expert_templates") is None


async def test_changed_role_at_commit_preserves_previous_assignment(env, monkeypatch):
    from octop.infra.db.repos.user_roles import UserRoleRepo
    from octop.infra.users import expert_assignment
    from octop.infra.users.expert_access import (
        POLICY_ALLOWED_EXPERT_TEMPLATES,
        POLICY_ASSIGNED_EXPERTS,
        account_expert_grants,
    )

    client, server, admin = env
    user = await client.post(
        "/api/users",
        headers=admin,
        json={
            "username": "role-race",
            "password": TEST_PASSWORD,
            "role": "user",
        },
    )
    uid = user.json()["id"]
    services = server.services
    roles = UserRoleRepo(services.db)
    original_policies = list(roles.get("user").policies)
    for template in ("old", "next"):
        services.agent_repo.create(
            agent_id=template, user_id=uid, name=template, published_expert_id=template
        )
    services.user_policy_repo.set(uid, POLICY_ASSIGNED_EXPERTS, '["old"]')
    path = f"/api/users/{uid}/expert-assignment"
    desired = {"templates": ["next"]}
    preview = await client.post(path + "/preview", headers=admin, json=desired)
    record = expert_assignment._record_change

    def change_role_before_commit(
        services, actor_id, user_id, updates, action, role_revision, **kwargs
    ):
        if action == "user.assign_experts":
            roles.update(
                "user", policies=[*original_policies, (POLICY_ALLOWED_EXPERT_TEMPLATES, '["old"]')]
            )
        return record(services, actor_id, user_id, updates, action, role_revision, **kwargs)

    monkeypatch.setattr(expert_assignment, "_record_change", change_role_before_commit)
    result = await client.put(
        path, headers=admin, json={**desired, "revision": preview.json()["revision"]}
    )
    assert result.status_code == 409, result.text
    assert services.audit_repo.query(action="user.assign_experts") == []
    roles.update("user", policies=original_policies)
    assert account_expert_grants(services.repos, uid) == {"old"}


async def test_install_receipt_failure_keeps_old_access_and_quota_until_retry(
    env_with_agent, monkeypatch
):
    import pytest

    from octop.infra.errors import OctopError
    from octop.infra.users.expert_access import POLICY_ASSIGNED_EXPERTS, assert_expert_invocation

    client, server, admin, source = env_with_agent
    published = await client.post(
        f"/api/agents/{source}/publish-expert", headers=admin, json={"name": "Replacement"}
    )
    template = published.json()["id"]
    user = await client.post(
        "/api/users",
        headers=admin,
        json={
            "username": "install-retry",
            "password": TEST_PASSWORD,
            "role": "user",
            "max_agents": 1,
        },
    )
    uid = user.json()["id"]
    services = server.services
    services.agent_repo.create(
        agent_id="original", user_id=uid, name="Original", published_expert_id="old"
    )
    path = f"/api/users/{uid}/expert-assignment"
    desired = {"templates": [template]}
    original = services.audit_repo.write_in_transaction

    def fail_final_audit(conn, **kwargs):
        if kwargs["action"] == "user.assign_experts":
            raise RuntimeError("audit unavailable")
        return original(conn, **kwargs)

    preview = (await client.post(path + "/preview", headers=admin, json=desired)).json()
    with monkeypatch.context() as scoped:
        scoped.setattr(services.audit_repo, "write_in_transaction", fail_final_audit)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await client.put(
                path,
                headers=admin,
                json={**desired, "revision": preview["revision"], "raise_limit_to": 2},
            )

    staged = (await client.post(path + "/preview", headers=admin, json=desired)).json()
    assert staged["ready"] == [template] and staged["current_limit"] == 1
    assert staged["needs_capacity_increase"] and staged["install"] == []
    assert services.user_policy_repo.get(uid, POLICY_ASSIGNED_EXPERTS) is None
    assert_expert_invocation(services.repos, "original", uid)
    created = staged["reuse"][template][0]
    with pytest.raises(OctopError):
        assert_expert_invocation(services.repos, created, uid)
    assert services.audit_repo.query(action="user.assign_experts") == []

    retry = {**desired, "revision": staged["revision"]}
    assert (await client.put(path, headers=admin, json=retry)).status_code == 409
    committed = await client.put(path, headers=admin, json={**retry, "raise_limit_to": 2})
    assert committed.status_code == 200, committed.text
    assert committed.json()["ready"] == [] and committed.json()["current_limit"] == 2
    assert committed.json()["reuse"][template] == [created]
    assert services.agent_repo.count_by_user(uid) == 2
    assert_expert_invocation(services.repos, created, uid)
    with pytest.raises(OctopError):
        assert_expert_invocation(services.repos, "original", uid)


async def _concurrency_case(env_with_agent: Any, username: str, max_agents: int) -> Any:
    from octop.infra.users.expert_access import POLICY_ASSIGNED_EXPERTS

    client, server, admin, source = env_with_agent
    published = await client.post(
        f"/api/agents/{source}/publish-expert", headers=admin, json={"name": "Concurrent"}
    )
    assert published.status_code == 201, published.text
    template = published.json()["id"]
    user = await client.post(
        "/api/users",
        headers=admin,
        json={
            "username": username,
            "password": TEST_PASSWORD,
            "role": "user",
            "max_agents": max_agents,
        },
    )
    assert user.status_code == 201, user.text
    uid = user.json()["id"]
    services = server.services
    services.agent_repo.create(
        agent_id=f"{username}-old", user_id=uid, name="Old", published_expert_id="old"
    )
    services.user_policy_repo.set(uid, POLICY_ASSIGNED_EXPERTS, '["old"]')
    path = f"/api/users/{uid}/expert-assignment"
    desired = {"templates": ["old", template]}
    preview = await client.post(path + "/preview", headers=admin, json=desired)
    assert preview.status_code == 200, preview.text
    return client, server, admin, uid, path, desired, preview.json()


def _patch_install(monkeypatch: Any, after_install: Any) -> Any:
    """Patch the installer; returns a callable that restores only this patch."""
    from octop.infra.agents.experts import default_agent

    original = default_agent.install_role_published_expert

    async def install_then_race(server: Any, **kwargs: Any) -> Any:
        created = await original(server, **kwargs)
        await after_install(server)
        return created

    monkeypatch.setattr(default_agent, "install_role_published_expert", install_then_race)
    return lambda: setattr(default_agent, "install_role_published_expert", original)


def _assert_old_assignment_kept(server: Any, uid: int, username: str, limit: int) -> None:
    import pytest

    from octop.infra.errors import OctopError
    from octop.infra.users.expert_access import (
        POLICY_ASSIGNED_EXPERTS,
        account_expert_grants,
        assert_expert_invocation,
    )
    from octop.infra.users.resource_policy import POLICY_MAX_AGENTS

    services = server.services
    assert services.user_policy_repo.get(uid, POLICY_MAX_AGENTS).value == str(limit)
    assert services.user_policy_repo.get(uid, POLICY_ASSIGNED_EXPERTS).value == '["old"]'
    assert account_expert_grants(services.repos, uid) == {"old"}
    assert_expert_invocation(services.repos, f"{username}-old", uid)
    assert services.audit_repo.query(action="user.assign_experts") == []
    others = [
        row.agent_id
        for row in services.agent_repo.list_by_user(uid)
        if row.agent_id != f"{username}-old" and row.published_expert_id
    ]
    for agent_id in others:
        with pytest.raises(OctopError):
            assert_expert_invocation(services.repos, agent_id, uid)


async def test_raise_conflicts_when_other_admin_raised_quota_during_install(
    env_with_agent, monkeypatch
):
    client, server, admin, uid, path, desired, plan = await _concurrency_case(
        env_with_agent, "race-raise", 1
    )
    assert plan["current_limit"] == 1 and plan["needs_capacity_increase"]

    async def other_admin_raises(server):
        await server.user_manager.set_max_agents("race-raise", 5)

    restore_install = _patch_install(monkeypatch, other_admin_raises)
    result = await client.put(
        path, headers=admin, json={**desired, "revision": plan["revision"], "raise_limit_to": 2}
    )
    assert result.status_code == 409, result.text
    assert result.json()["error"]["code"] == "EXPERT_ASSIGNMENT_CONFLICT", result.text
    _assert_old_assignment_kept(server, uid, "race-raise", 5)

    from octop.infra.users.expert_access import assert_expert_invocation

    restore_install()  # keep the autouse fixtures' monkeypatches (HOME, scheduler, env)
    services = server.services
    count_after_conflict = services.agent_repo.count_by_user(uid)
    retry_plan = (await client.post(path + "/preview", headers=admin, json=desired)).json()
    assert retry_plan["install"] == [] and retry_plan["current_limit"] == 5
    assert not retry_plan["needs_capacity_increase"]
    retried = await client.put(
        path, headers=admin, json={**desired, "revision": retry_plan["revision"]}
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["current_limit"] == 5
    assert services.agent_repo.count_by_user(uid) == count_after_conflict == 2
    created = retried.json()["reuse"][plan["install"][0]][0]
    assert_expert_invocation(services.repos, created, uid)
    assert len(services.audit_repo.query(action="user.assign_experts")) == 1


async def test_assignment_conflicts_when_other_admin_lowered_quota_during_install(
    env_with_agent, monkeypatch
):
    client, server, admin, uid, path, desired, plan = await _concurrency_case(
        env_with_agent, "race-lower", 2
    )
    assert plan["current_limit"] == 2 and not plan["needs_capacity_increase"]

    async def other_admin_lowers(server):
        await server.user_manager.set_max_agents("race-lower", 1)

    _patch_install(monkeypatch, other_admin_lowers)
    result = await client.put(path, headers=admin, json={**desired, "revision": plan["revision"]})
    assert result.status_code == 409, result.text
    assert result.json()["error"]["code"] == "EXPERT_ASSIGNMENT_CONFLICT", result.text
    _assert_old_assignment_kept(server, uid, "race-lower", 1)


async def test_assignment_conflicts_when_concurrent_instance_takes_last_slot(
    env_with_agent, monkeypatch
):
    client, server, admin, uid, path, desired, plan = await _concurrency_case(
        env_with_agent, "race-slot", 1
    )

    async def concurrent_create(server):
        server.services.agent_repo.create(agent_id="race-slot-extra", user_id=uid, name="Extra")

    _patch_install(monkeypatch, concurrent_create)
    result = await client.put(
        path, headers=admin, json={**desired, "revision": plan["revision"], "raise_limit_to": 2}
    )
    assert result.status_code == 409, result.text
    assert result.json()["error"]["code"] == "AGENT_QUOTA_EXCEEDED", result.text
    _assert_old_assignment_kept(server, uid, "race-slot", 1)


def _age_reservation(server: Any, uid: int, template: str) -> None:
    """Move an installing key past its lease, as if the installer died that long ago."""
    from octop.infra.users.expert_access import INSTALLING_EXPERT_PREFIX
    from octop.infra.users.expert_assignment import INSTALL_LEASE_SECONDS

    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE user_policies SET updated_at = updated_at - ? WHERE user_id = ? AND name = ?",
            (INSTALL_LEASE_SECONDS + 1, uid, INSTALLING_EXPERT_PREFIX + template),
        )


async def _crash_before_ready(env_with_agent: Any, monkeypatch: Any, username: str) -> Any:
    """Apply one new template; the installer finishes but the process dies before READY."""
    import pytest

    client, server, admin, uid, path, desired, plan = await _concurrency_case(
        env_with_agent, username, 2
    )

    async def process_dies(server: Any) -> None:
        raise RuntimeError("process died before READY")

    restore = _patch_install(monkeypatch, process_dies)
    with pytest.raises(RuntimeError, match="before READY"):
        await client.put(path, headers=admin, json={**desired, "revision": plan["revision"]})
    restore()
    return client, server, admin, uid, path, desired, plan["install"][0]


def _audit_count(server: Any, action: str) -> int:
    return len(server.services.audit_repo.query(action=action))


async def test_interrupted_install_is_verified_discarded_and_reinstalled(
    env_with_agent, monkeypatch
):
    import pytest

    from octop.infra.errors import OctopError
    from octop.infra.users.expert_access import assert_expert_invocation

    client, server, admin, uid, path, desired, template = await _crash_before_ready(
        env_with_agent, monkeypatch, "crash-install"
    )
    services = server.services
    preview = (await client.post(path + "/preview", headers=admin, json=desired)).json()
    assert preview["conflicts"] == {template: "installation_unconfirmed"}
    pending = preview["pending"][template]
    assert pending["state"] == "installing"
    orphan = pending["agent_id"]
    assert services.agent_repo.get(orphan) is not None
    assert services.agent_repo.count_by_user(uid) == 2
    with pytest.raises(OctopError):
        assert_expert_invocation(services.repos, orphan, uid)

    discard = path + "/discard"
    body = {"template": template, "agent_id": orphan}
    customer = bearer(await login(client, username="crash-install"))
    assert (await client.post(discard, headers=customer, json=body)).status_code == 403
    early = await client.post(discard, headers=admin, json=body)
    assert early.status_code == 409, early.text
    assert early.json()["error"]["details"] == {"discardable_at": pending["discardable_at"]}
    _age_reservation(server, uid, template)
    wrong = await client.post(discard, headers=admin, json={**body, "agent_id": "not-reserved"})
    assert wrong.status_code == 409, wrong.text
    assert services.agent_repo.get(orphan) is not None

    workspace = server.app_runtime.agent_registry.resolve_workspace_dir(
        orphan, persist_if_missing=False
    )
    assert workspace.exists()
    done = await client.post(discard, headers=admin, json=body)
    assert done.status_code == 200, done.text
    assert done.json() == {**body, "deleted_instance": True}
    assert services.agent_repo.get(orphan) is None and not workspace.exists()
    assert services.agent_repo.count_by_user(uid) == 1
    _assert_old_assignment_kept(server, uid, "crash-install", 2)
    assert _audit_count(server, "user.discard_unconfirmed_expert") == 1

    retry = (await client.post(path + "/preview", headers=admin, json=desired)).json()
    assert retry["install"] == [template] and retry["pending"] == {} and not retry["conflicts"]
    applied = await client.put(path, headers=admin, json={**desired, "revision": retry["revision"]})
    assert applied.status_code == 200, applied.text
    created = applied.json()["reuse"][template][0]
    assert created != orphan and applied.json()["pending"] == {}
    assert_expert_invocation(services.repos, created, uid)
    assert services.agent_repo.count_by_user(uid) == 2


async def test_ready_instance_left_by_conflict_is_released_without_touching_assignment(
    env_with_agent, monkeypatch
):
    client, server, admin, uid, path, desired, plan = await _concurrency_case(
        env_with_agent, "orphan-ready", 2
    )
    template = plan["install"][0]

    async def other_admin_lowers(server: Any) -> None:
        await server.user_manager.set_max_agents("orphan-ready", 1)

    restore = _patch_install(monkeypatch, other_admin_lowers)
    result = await client.put(path, headers=admin, json={**desired, "revision": plan["revision"]})
    assert result.status_code == 409, result.text
    restore()

    # Nobody retries the new template; the admin only looks at the account again.
    kept = {"templates": ["old"]}
    preview = (await client.post(path + "/preview", headers=admin, json=kept)).json()
    orphan = preview["pending"][template]
    assert orphan["state"] == "ready" and orphan["discardable_at"] == orphan["since"]
    assert preview["current_limit"] == 1 and preview["required_capacity"] == 2
    # A discard only frees capacity, so a limit lowered below what is owned must not block it.
    await server.user_manager.set_max_agents("orphan-ready", 0)
    done = await client.post(
        path + "/discard",
        headers=admin,
        json={"template": template, "agent_id": orphan["agent_id"]},
    )
    assert done.status_code == 200, done.text
    services = server.services
    assert services.agent_repo.get(orphan["agent_id"]) is None
    assert services.agent_repo.count_by_user(uid) == 1
    _assert_old_assignment_kept(server, uid, "orphan-ready", 0)
    after = (await client.post(path + "/preview", headers=admin, json=kept)).json()
    assert after["pending"] == {} and after["required_capacity"] == 1
    assert _audit_count(server, "user.expert_install_discarding") == 1
    assert _audit_count(server, "user.discard_unconfirmed_expert") == 1


async def test_discard_interrupted_midway_stays_blocked_then_recovers(env_with_agent, monkeypatch):
    import pytest

    from octop.infra.errors import OctopError
    from octop.infra.users import expert_assignment
    from octop.infra.users.expert_access import assert_expert_invocation

    client, server, admin, uid, path, desired, template = await _crash_before_ready(
        env_with_agent, monkeypatch, "discard-crash"
    )
    services = server.services
    preview = (await client.post(path + "/preview", headers=admin, json=desired)).json()
    orphan = preview["pending"][template]["agent_id"]
    _age_reservation(server, uid, template)
    discard = path + "/discard"
    body = {"template": template, "agent_id": orphan}
    registry = server.app_runtime.agent_registry

    async def delete_interrupted(agent_id: str) -> None:
        raise RuntimeError("delete interrupted")

    with monkeypatch.context() as scoped:
        scoped.setattr(registry, "delete", delete_interrupted)
        with pytest.raises(RuntimeError, match="delete interrupted"):
            await client.post(discard, headers=admin, json=body)
    marked = (await client.post(path + "/preview", headers=admin, json=desired)).json()
    assert marked["conflicts"] == {template: "installation_discarding"}
    assert marked["pending"][template]["state"] == "discarding"
    assert services.agent_repo.get(orphan) is not None
    with pytest.raises(OctopError):
        assert_expert_invocation(services.repos, orphan, uid)

    record = expert_assignment._record_change

    def clear_interrupted(*args: Any, **kwargs: Any) -> None:
        if args[4] == "user.discard_unconfirmed_expert":
            raise RuntimeError("clear interrupted")
        record(*args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(expert_assignment, "_record_change", clear_interrupted)
        with pytest.raises(RuntimeError, match="clear interrupted"):
            await client.post(discard, headers=admin, json=body)
    assert services.agent_repo.get(orphan) is None
    gone = (await client.post(path + "/preview", headers=admin, json=desired)).json()
    assert gone["install"] == [template] and not gone["conflicts"]
    assert gone["pending"][template]["state"] == "discarding"

    # The next apply replaces the reservation with a fresh ID and confirms that one.
    applied = await client.put(path, headers=admin, json={**desired, "revision": gone["revision"]})
    assert applied.status_code == 200, applied.text
    created = applied.json()["reuse"][template][0]
    assert created != orphan and applied.json()["pending"] == {}
    assert_expert_invocation(services.repos, created, uid)
    assert services.agent_repo.count_by_user(uid) == 2
    # A discard of the old reservation still running in another process cannot reach it:
    # its delete targets the old ID and its clear no longer matches the key.
    with pytest.raises(OctopError):
        await registry.delete(orphan)
    assert (await client.post(discard, headers=admin, json=body)).status_code == 409
    assert services.agent_repo.get(created) is not None
    assert_expert_invocation(services.repos, created, uid)


async def test_installer_stalled_past_its_lease_cannot_insert_after_discard(
    env_with_agent, monkeypatch
):
    """The installer claims, then stalls before inserting until its lease runs out; another
    process discards the claim. The late insert is refused and nothing holds a slot."""
    from octop.infra.agents.experts import default_agent
    from octop.infra.users.expert_access import INSTALLING_EXPERT_PREFIX
    from octop.infra.users.expert_assignment import ExpertAssignments

    client, server, admin, uid, path, desired, plan = await _concurrency_case(
        env_with_agent, "stalled-install", 2
    )
    template = plan["install"][0]
    services = server.services
    admin_id = next(row.id for row in services.user_repo.list() if row.role == "admin")
    original = default_agent.install_role_published_expert
    receipts: list[Any] = []

    async def stall_past_lease_then_insert(server: Any, **kwargs: Any) -> Any:
        _age_reservation(server, uid, template)
        # Another server process: its own ExpertAssignments, so not this apply's lock.
        other = ExpertAssignments()
        receipts.append(await other.discard(server, uid, template, kwargs["agent_id"], admin_id))
        return await original(server, **kwargs)

    monkeypatch.setattr(
        default_agent, "install_role_published_expert", stall_past_lease_then_insert
    )
    result = await client.put(path, headers=admin, json={**desired, "revision": plan["revision"]})
    assert result.status_code == 409, result.text
    assert result.json()["error"]["code"] == "EXPERT_ASSIGNMENT_CONFLICT", result.text
    assert receipts[0].deleted_instance is False
    assert services.agent_repo.get(receipts[0].agent_id) is None
    assert services.agent_repo.count_by_user(uid) == 1
    key = services.user_policy_repo.get(uid, INSTALLING_EXPERT_PREFIX + template)
    assert key is None or not key.enabled
    _assert_old_assignment_kept(server, uid, "stalled-install", 2)


async def test_create_counts_quota_inside_its_insert_transaction(env_with_agent, monkeypatch):
    """Another process takes the last slot after ``create`` starts, before it inserts."""
    import pytest

    from octop.infra.agents.manager import AgentCreateSpec
    from octop.infra.agents.workspace import dir as workspace_dir
    from octop.infra.errors import ErrorCode, OctopError

    client, server, admin, _ = env_with_agent
    user = await client.post(
        "/api/users",
        headers=admin,
        json={"username": "quota-race", "password": TEST_PASSWORD, "role": "user", "max_agents": 2},
    )
    assert user.status_code == 201, user.text
    uid = user.json()["id"]
    services = server.services
    services.agent_repo.create(agent_id="quota-race-first", user_id=uid, name="First")
    seed = workspace_dir.seed_workspace_dir_on_create

    def other_process_takes_last_slot(config: Any, **kwargs: Any) -> Any:
        if services.agent_repo.get("quota-race-other") is None:
            services.agent_repo.create(agent_id="quota-race-other", user_id=uid, name="Other")
        return seed(config, **kwargs)

    monkeypatch.setattr(
        workspace_dir, "seed_workspace_dir_on_create", other_process_takes_last_slot
    )
    with pytest.raises(OctopError) as caught:
        await server.app_runtime.agent_registry.create(AgentCreateSpec(name="Late", user_id=uid))
    assert caught.value.code == ErrorCode.AGENT_QUOTA_EXCEEDED
    assert caught.value.details == {"used": 2, "quota": 2}
    assert [row.name for row in services.agent_repo.list_by_user(uid)] == ["First", "Other"]


async def test_replacing_a_reservation_fails_if_its_installer_inserted_since(
    env_with_agent, monkeypatch
):
    """Preview saw the reservation without an instance; its installer inserts before this
    apply claims. The claim must not take the reservation away from that instance."""
    import pytest

    from octop.infra.errors import OctopError
    from octop.infra.users import expert_assignment
    from octop.infra.users.expert_access import INSTALLING_EXPERT_PREFIX, assert_expert_invocation

    client, server, admin, uid, path, desired, _ = await _concurrency_case(
        env_with_agent, "late-insert", 2
    )
    services = server.services
    template = desired["templates"][1]
    key = INSTALLING_EXPERT_PREFIX + template
    services.user_policy_repo.set(uid, key, "stalled")
    plan = (await client.post(path + "/preview", headers=admin, json=desired)).json()
    assert plan["install"] == [template] and plan["pending"][template]["agent_id"] == "stalled"
    stage = expert_assignment._stage

    def installer_inserts_first(*args: Any) -> Any:
        services.agent_repo.create(
            agent_id="stalled", user_id=uid, name="Stalled", published_expert_id=template
        )
        return stage(*args)

    monkeypatch.setattr(expert_assignment, "_stage", installer_inserts_first)
    result = await client.put(path, headers=admin, json={**desired, "revision": plan["revision"]})
    assert result.status_code == 409, result.text
    assert services.user_policy_repo.get(uid, key).value == "stalled"
    with pytest.raises(OctopError):
        assert_expert_invocation(services.repos, "stalled", uid)
    after = (await client.post(path + "/preview", headers=admin, json=desired)).json()
    assert after["conflicts"] == {template: "installation_unconfirmed"}


async def test_discard_deletes_an_instance_inserted_just_before_its_mark(
    env_with_agent, monkeypatch
):
    """No instance when the discard first looks; the installer inserts before the mark."""
    from octop.infra.users import expert_assignment
    from octop.infra.users.expert_access import INSTALLING_EXPERT_PREFIX

    client, server, admin, uid, path, desired, _ = await _concurrency_case(
        env_with_agent, "mark-race", 2
    )
    services = server.services
    template = desired["templates"][1]
    services.user_policy_repo.set(uid, INSTALLING_EXPERT_PREFIX + template, "late")
    _age_reservation(server, uid, template)
    record = expert_assignment._record_change

    def installer_inserts_before_mark(*args: Any, **kwargs: Any) -> None:
        if args[4] == "user.expert_install_discarding":
            services.agent_repo.create(
                agent_id="late", user_id=uid, name="Late", published_expert_id=template
            )
        record(*args, **kwargs)

    monkeypatch.setattr(expert_assignment, "_record_change", installer_inserts_before_mark)
    done = await client.post(
        path + "/discard", headers=admin, json={"template": template, "agent_id": "late"}
    )
    assert done.status_code == 200, done.text
    assert done.json()["deleted_instance"] is True
    assert services.agent_repo.get("late") is None
    assert services.agent_repo.count_by_user(uid) == 1


async def test_discard_never_deletes_an_instance_of_another_account(env_with_agent, monkeypatch):
    """The reserved ID names another account's instance, seen before the mark or only
    after it: the discard refuses and leaves that instance alone."""
    from octop.infra.users import expert_assignment
    from octop.infra.users.expert_access import DISCARDING_PREFIX, INSTALLING_EXPERT_PREFIX

    client, server, admin, uid, path, desired, _ = await _concurrency_case(
        env_with_agent, "foreign-id", 2
    )
    services = server.services
    template = desired["templates"][1]
    key = INSTALLING_EXPERT_PREFIX + template
    other = services.user_repo.create(username="foreign-owner", password_hash="h", role="user")
    discard = path + "/discard"

    services.agent_repo.create(
        agent_id="taken", user_id=other, name="Taken", published_expert_id=template
    )
    services.user_policy_repo.set(uid, key, "taken")
    _age_reservation(server, uid, template)
    before = await client.post(
        discard, headers=admin, json={"template": template, "agent_id": "taken"}
    )
    assert before.status_code == 409, before.text
    assert services.agent_repo.get("taken") is not None
    assert services.user_policy_repo.get(uid, key).value == "taken"

    services.user_policy_repo.set(uid, key, "appears")
    _age_reservation(server, uid, template)
    record = expert_assignment._record_change

    def foreign_insert_before_mark(*args: Any, **kwargs: Any) -> None:
        if args[4] == "user.expert_install_discarding":
            services.agent_repo.create(
                agent_id="appears", user_id=other, name="Appears", published_expert_id=template
            )
        record(*args, **kwargs)

    monkeypatch.setattr(expert_assignment, "_record_change", foreign_insert_before_mark)
    after = await client.post(
        discard, headers=admin, json={"template": template, "agent_id": "appears"}
    )
    assert after.status_code == 409, after.text
    assert services.agent_repo.get("appears") is not None
    assert services.user_policy_repo.get(uid, key).value == DISCARDING_PREFIX + "appears"


async def test_discard_for_a_missing_account_is_not_found(env_with_agent):
    client, _, admin, _ = env_with_agent
    missing = await client.post(
        "/api/users/999999/expert-assignment/discard",
        headers=admin,
        json={"template": "any", "agent_id": "any"},
    )
    assert missing.status_code == 404, missing.text
    assert missing.json()["error"]["code"] == "NOT_FOUND", missing.text


async def test_discard_reports_an_instance_another_process_deleted_first(
    env_with_agent, monkeypatch
):
    from octop.infra.users.expert_access import INSTALLING_EXPERT_PREFIX

    client, server, admin, uid, path, desired, template = await _crash_before_ready(
        env_with_agent, monkeypatch, "deleted-first"
    )
    services = server.services
    preview = (await client.post(path + "/preview", headers=admin, json=desired)).json()
    orphan = preview["pending"][template]["agent_id"]
    _age_reservation(server, uid, template)
    registry = server.app_runtime.agent_registry
    delete = registry.delete

    async def other_process_deletes_first(agent_id: str) -> None:
        await delete(agent_id)  # the other process
        await delete(agent_id)  # this call, which now finds nothing

    monkeypatch.setattr(registry, "delete", other_process_deletes_first)
    body = {"template": template, "agent_id": orphan}
    done = await client.post(path + "/discard", headers=admin, json=body)
    assert done.status_code == 200, done.text
    assert done.json() == {**body, "deleted_instance": False}
    assert services.agent_repo.get(orphan) is None
    key = services.user_policy_repo.get(uid, INSTALLING_EXPERT_PREFIX + template)
    assert key is None or not key.enabled


async def test_create_over_quota_refuses_before_creating_a_custom_workspace(
    env_with_agent, tmp_path
):
    import pytest

    from octop.infra.agents.manager import AgentCreateSpec
    from octop.infra.errors import ErrorCode, OctopError

    client, server, admin, _ = env_with_agent
    user = await client.post(
        "/api/users",
        headers=admin,
        json={"username": "full-quota", "password": TEST_PASSWORD, "role": "user", "max_agents": 1},
    )
    assert user.status_code == 201, user.text
    uid = user.json()["id"]
    server.services.agent_repo.create(agent_id="full-quota-first", user_id=uid, name="First")
    workspace = tmp_path / "custom-workspace"
    spec = AgentCreateSpec(name="Over", user_id=uid, config={"workspace_dir": str(workspace)})
    with pytest.raises(OctopError) as caught:
        await server.app_runtime.agent_registry.create(spec)
    assert caught.value.code == ErrorCode.AGENT_QUOTA_EXCEEDED
    assert not workspace.exists()


async def test_create_runs_its_quota_transaction_off_the_event_loop(env_with_agent, monkeypatch):
    """That transaction can wait on another process's row lock; the loop must keep serving."""
    import threading

    from octop.infra.agents.manager import AgentCreateSpec
    from octop.infra.users import resource_policy

    client, server, admin, _ = env_with_agent
    user = await client.post(
        "/api/users",
        headers=admin,
        json={"username": "off-loop", "password": TEST_PASSWORD, "role": "user", "max_agents": 2},
    )
    assert user.status_code == 201, user.text
    original = resource_policy.create_expert_within_quota
    threads: list[int] = []

    def record_thread(*args: Any, **kwargs: Any) -> None:
        threads.append(threading.get_ident())
        original(*args, **kwargs)

    monkeypatch.setattr(resource_policy, "create_expert_within_quota", record_thread)
    spec = AgentCreateSpec(name="Off loop", user_id=user.json()["id"])
    row = await server.app_runtime.agent_registry.create(spec)
    assert server.services.agent_repo.get(row.agent_id) is not None
    assert threads and threads[0] != threading.get_ident()
