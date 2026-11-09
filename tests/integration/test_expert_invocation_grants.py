"""Role template grants are re-read at execution, independent of UI defaults."""

import pytest

from octop.api.routers.chat.models import ChatTurnBody
from octop.api.routers.chat.turn import prepare_dashboard_turn
from octop.infra.errors import ErrorCode, OctopError
from tests.support.auth import TEST_PASSWORD, bearer, login


async def test_unlisted_expert_denied_before_thread_or_harness_work(env):
    client, server, admin = env
    created = await client.post(
        "/api/users/roles",
        headers=admin,
        json={
            "user_role_name": "Customer expert grants",
            "permissions": ["experts"],
            "policies": [{"name": "allowed_expert_templates", "value": '["approved-template"]'}],
        },
    )
    assert created.status_code == 201, created.text
    role = created.json()["user_role_id"]
    result = await client.post(
        "/api/users",
        headers=admin,
        json={"username": "expert_customer", "password": TEST_PASSWORD, "role": role},
    )
    assert result.status_code == 201, result.text
    uid = result.json()["id"]
    server.services.agent_repo.create(agent_id="denied-agent", user_id=uid, name="Unlisted")
    server.services.agent_repo.create(
        agent_id="approved-agent",
        user_id=uid,
        name="Approved",
        published_expert_id="approved-template",
    )
    registry = server.app_runtime.agent_registry
    await registry.assert_user_can_invoke("approved-agent", uid)
    customer_auth = bearer(await login(client, username="expert_customer"))
    for method, url, body in (
        ("PATCH", "/api/agents/approved-agent", {"system_prompt": "replace curated instructions"}),
        ("POST", "/api/agents", {"name": "unrestricted replacement"}),
        ("POST", "/api/experts/hub/other/install", {}),
    ):
        response = await client.request(method, url, headers=customer_auth, json=body)
        assert response.status_code == 403, response.text
    assert server.services.agent_repo.get("approved-agent").system_prompt is None
    user = server.user_manager.get_by_id(uid)
    with pytest.raises(OctopError) as denied:
        await prepare_dashboard_turn(
            server, agent_id="denied-agent", user=user, turn=ChatTurnBody(text="hello")
        )
    assert denied.value.code == ErrorCode.FORBIDDEN
    updated = await client.patch(
        f"/api/users/roles/{role}",
        headers=admin,
        json={"policies": [{"name": "allowed_expert_templates", "value": "[]"}]},
    )
    assert updated.status_code == 200
    # Simulate a message accepted while allowed, then dequeued after revocation.
    from unittest.mock import MagicMock, patch

    from octop_gateway.models import InboundMessage, TextContent

    from octop.infra.gateway.process.processor import GlobalProcessor

    processor = GlobalProcessor(
        agent_manager=registry,
        thread_registry=MagicMock(),
        audit_repo=server.services.audit_repo,
        agent_repo=server.services.agent_repo,
        user_repo=server.services.user_repo,
        connector_repo=server.services.connector_repo,
        dispatcher=MagicMock(),
    )
    queued = InboundMessage(
        channel_id="dashboard",
        channel_type="dashboard",
        tenant_id="approved-agent",
        content=[TextContent(text="queued request")],
    )
    with patch.object(registry, "stream") as harness:
        chunks = [chunk async for chunk in processor.iter_turn_chunks(queued)]
        assert [chunk["type"] for chunk in chunks] == ["error", "done"]
        harness.assert_not_called()
    with pytest.raises(OctopError):
        await registry.assert_user_can_invoke("approved-agent", uid)
    # Removing the field via an old dashboard must not restore unrestricted access.
    await client.patch(f"/api/users/roles/{role}", headers=admin, json={"policies": []})
    with pytest.raises(OctopError):
        await registry.assert_user_can_invoke("approved-agent", uid)
    assert server.services.agent_repo.get("approved-agent") is not None

    # Direct execution and paused-turn resumption cannot bypass the same policy.
    with patch.object(registry._harness_manager, "call") as call_model:
        with pytest.raises(OctopError) as direct:
            await registry.call("approved-agent", {"user": str(uid)})
        assert direct.value.code == ErrorCode.FORBIDDEN
        call_model.assert_not_called()
    with pytest.raises(OctopError):
        _ = [chunk async for chunk in registry.stream("approved-agent", {"user": str(uid)})]
    with pytest.raises(OctopError):
        _ = [chunk async for chunk in registry.resume_hitl("approved-agent", "pending-turn", [])]
    assert not registry._active_invocations
    # Nested thread ids must resolve the thread principal, not the admin owner.
    server.services.agent_repo.create(agent_id="admin-owned", user_id=1, name="Shared internal")
    server.services.thread_repo.insert(
        thread_id="customer-thread",
        agent_id="admin-owned",
        user_id=uid,
        channel_type="dashboard",
        session_key="customer-session",
    )
    with patch.object(registry._harness_manager, "call") as call_model:
        with pytest.raises(OctopError) as nested:
            await registry.call("admin-owned", {"configurable": {"thread_id": "customer-thread"}})
        assert nested.value.code == ErrorCode.FORBIDDEN
        call_model.assert_not_called()
    # Mid-stream revocation must cancel and await producer cleanup before release.
    import asyncio

    for method in ("stream", "resume_hitl"):
        await client.patch(
            f"/api/users/roles/{role}",
            headers=admin,
            json={
                "policies": [{"name": "allowed_expert_templates", "value": '["approved-template"]'}]
            },
        )
        closed = []

        async def producer(*args, _closed=closed, **kwargs):
            try:
                yield {"type": "token", "content": "allowed"}
                yield {"type": "token", "content": "must not deliver"}
            finally:
                await asyncio.sleep(0)
                # Cleanup must occur while invocation tracking is still held.
                _closed.append(registry.is_agent_active("approved-agent"))

        from types import SimpleNamespace

        from octop.infra.agents.harness_manager import ClosingHarnessAgentManager

        # Use the production wrapper; only the innermost model/agent is fake.
        harness = ClosingHarnessAgentManager.__new__(ClosingHarnessAgentManager)
        cancel = MagicMock()
        inner = SimpleNamespace(stream=producer, resume_hitl=producer, cancel=cancel)
        harness._registry = {"approved-agent": SimpleNamespace(agent=inner)}
        harness._cancel_events = {}
        harness._langfuse = None
        with patch.object(registry, "_harness_manager", harness):
            stream = (
                registry.stream(
                    "approved-agent",
                    {"user": str(uid), "thread_id": "revocation-turn", "messages": []},
                )
                if method == "stream"
                else registry.resume_hitl("approved-agent", "revocation-turn", [])
            )
            assert (await anext(stream))["content"] == "allowed"
            await client.patch(
                f"/api/users/roles/{role}",
                headers=admin,
                json={"policies": [{"name": "allowed_expert_templates", "value": "[]"}]},
            )
            with pytest.raises(OctopError):
                await anext(stream)
            cancel.assert_called_once_with("revocation-turn")
            assert closed == [True]
            assert not registry.is_agent_active("approved-agent")
            assert not registry._thread_execution_lock("approved-agent", "revocation-turn").locked()
