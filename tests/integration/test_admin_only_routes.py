"""Stored keys, host commands and Octop's own data stay with admins.

Non-admin accounts keep chatting, using their agents' workspaces and the browser
panel; they see masked provider keys and cannot define commands the server runs,
browse host directories, point a bridge somewhere new or open admin-only host paths.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.support.auth import create_agent, create_user, resolve_user_id

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX system paths")


@pytest.fixture
async def staff(env_with_provider: Any) -> Any:
    c, srv, admin = env_with_provider
    user = await create_user(c, admin, username="staff")
    yield c, srv, admin, user


async def test_provider_and_voice_keys_are_masked_for_non_admins(staff: Any) -> None:
    c, _srv, admin, user = staff
    made = await c.post(
        "/api/admin/providers",
        headers=admin,
        json={"name": "masked", "kind": "openai", "api_key": "sk-abcdef123456"},
    )
    assert made.status_code == 201, made.text
    voice = await c.post(
        "/api/admin/voice/providers",
        headers=admin,
        json={
            "name": "asr",
            "kind": "tencent",
            "capability": "both",
            "api_key": "sid:secretvalue",
            "extra_json": json.dumps(
                {"secret_id": "AKIDabcdef", "secret_key": "verysecret", "region": "ap-guangzhou"}
            ),
        },
    )
    assert voice.status_code == 201, voice.text

    def _row(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
        return next(r for r in rows if r["name"] == name)

    seen = _row((await c.get("/api/providers", headers=user)).json(), "masked")
    assert seen["api_key"] == "sk-a********"
    full = _row((await c.get("/api/providers", headers=admin)).json(), "masked")
    assert full["api_key"] == "sk-abcdef123456"

    seen_voice = _row((await c.get("/api/voice/providers", headers=user)).json(), "asr")
    assert seen_voice["api_key"] == "sid:********"
    assert seen_voice["extra"]["secret_id"] == "AKID********"
    assert seen_voice["extra"]["secret_key"] == "very********"
    assert seen_voice["extra"]["region"] == "ap-guangzhou"
    full_voice = _row((await c.get("/api/voice/providers", headers=admin)).json(), "asr")
    assert full_voice["extra"]["secret_key"] == "verysecret"


async def test_non_admins_cannot_define_server_commands(staff: Any) -> None:
    c, _srv, admin, user = staff
    stdio = {"transport": "stdio", "command": "true"}
    http = {"transport": "streamable_http", "url": "https://mcp.example.com/mcp"}

    for body in ({"servers": {"cmd": stdio}}, {"servers": {"web": {**http, "shared": True}}}):
        denied = await c.put("/api/connectors/custom-mcp", headers=user, json=body)
        assert denied.status_code == 403, denied.text
    probe = await c.post("/api/connectors/custom-mcp/test", headers=user, json={"server": stdio})
    assert probe.status_code == 403, probe.text
    own = await c.put("/api/connectors/custom-mcp", headers=user, json={"servers": {"web": http}})
    assert own.status_code == 200, own.text
    assert (
        await c.put("/api/connectors/custom-mcp", headers=admin, json={"servers": {"cmd": stdio}})
    ).status_code == 200

    aid = await create_agent(c, user, name="staff-bot")
    runner = {"enabled": True, "command": "true"}
    for method, path, body in (
        ("PUT", f"/api/agents/{aid}/acp", {"runners": {"evil": runner}}),
        ("PUT", f"/api/agents/{aid}/acp/evil", runner),
        ("DELETE", f"/api/agents/{aid}/acp/evil", None),
    ):
        denied = await c.request(method, path, headers=user, json=body)
        assert denied.status_code == 403, (path, denied.text)
    assert (await c.get(f"/api/agents/{aid}/acp", headers=user)).status_code == 200


async def test_bridge_new_targets_are_admin_only(staff: Any) -> None:
    c, srv, admin, user = staff
    peer = {"peer_base_url": "https://peer.example", "peer_username": "u", "password": "p"}
    assert (await c.post("/api/bridge/probe", headers=user, json=peer)).status_code == 403
    created = await c.post(
        "/api/bridge/connections", headers=user, json={**peer, "display_name": "Peer"}
    )
    assert created.status_code == 403, created.text

    uid = await resolve_user_id(c, admin, "staff")
    srv.services.bridge_connection_repo.create(
        connection_id="conn-staff",
        owner_user_id=uid,
        peer_base_url="https://peer.example",
        peer_username="u",
        display_name="Peer",
    )
    base = "/api/bridge/connections/conn-staff"
    # The edit form resends the address and account unchanged with a new name.
    renamed = await c.patch(
        base,
        headers=user,
        json={
            "display_name": "Renamed",
            "peer_base_url": "https://peer.example/",
            "peer_username": "u",
        },
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["display_name"] == "Renamed"
    for change in (
        {"peer_base_url": "https://elsewhere.example"},
        {"peer_username": "other"},
        {"password": "new"},
    ):
        denied = await c.patch(base, headers=user, json=change)
        assert denied.status_code == 403, (change, denied.text)
    probe = await c.post(f"{base}/probe", headers=user, json=peer)
    assert probe.status_code == 403, probe.text
    assert (await c.delete(base, headers=user)).status_code in (200, 204)


async def test_browser_shutdown_is_admin_only(staff: Any) -> None:
    c, _srv, _admin, user = staff
    assert (await c.post("/api/browser/shutdown", headers=user)).status_code == 403


@posix_only
async def test_workspace_routes_keep_admin_only_host_paths(staff: Any, tmp_path: Path) -> None:
    c, srv, admin, user = staff
    registry = srv.app_runtime.agent_registry
    mine = await create_agent(c, user, name="staff-bot")
    other = await create_agent(c, admin, name="admin-bot")
    await registry.get_agent(other).workspace.aupload_bytes("secret.txt", b"admin only")
    other_dir = Path(registry.get_agent(other).workspace.workspace_dir)
    ws = f"/api/agents/{mine}/workspace"

    put = await c.put(
        f"{ws}/file?path=%2Fnotes.md&from_workspace=true", headers=user, json={"content": "hi"}
    )
    assert put.status_code == 200, put.text
    got = await c.get(f"{ws}/file?path=%2Fnotes.md&from_workspace=true", headers=user)
    assert got.status_code == 200, got.text

    scratch = tmp_path / "scratch.txt"
    scratch.write_text("shared scratch", encoding="utf-8")
    ok = await c.get(f"{ws}/file", headers=user, params={"path": scratch.as_posix()})
    assert ok.status_code == 200, ok.text

    for path in (
        "/etc/hosts",
        (other_dir / "secret.txt").as_posix(),
        f"./ {(other_dir / 'secret.txt').as_posix()}",
        f"file://{(other_dir / 'secret.txt').as_posix()}",
    ):
        denied = await c.get(f"{ws}/file", headers=user, params={"path": path})
        assert denied.status_code == 403, (path, denied.text)
    tree = await c.get(f"{ws}/tree", headers=user, params={"path": other_dir.parent.as_posix()})
    assert tree.status_code == 403, tree.text
    write = await c.put(
        f"{ws}/file",
        headers=user,
        params={"path": (other_dir / "planted.txt").as_posix()},
        json={"content": "x"},
    )
    assert write.status_code == 403, write.text
    assert not (other_dir / "planted.txt").exists()

    listed = await c.get(f"{ws}/tree", headers=user, params={"path": tmp_path.as_posix()})
    assert listed.status_code == 200, listed.text
    assert any("scratch.txt" in row["path"] for row in listed.json())

    admin_ws = f"/api/agents/{other}/workspace"
    admin_read = await c.get(
        f"{admin_ws}/file", headers=admin, params={"path": (other_dir / "secret.txt").as_posix()}
    )
    assert admin_read.status_code == 200, admin_read.text
