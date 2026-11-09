"""Role grants must protect plugin assets, including conditional downloads."""

import json

from tests.support.auth import TEST_PASSWORD, bearer, login


async def test_plugin_ui_role_revocation_blocks_cached_download(env):
    client, _server, admin = env
    catalog = (await client.get("/api/plugins/market", headers=admin)).json()
    plugin = next(p["id"] for p in catalog if not p.get("error"))
    installed = await client.post(f"/api/plugins/market/{plugin}/install", headers=admin)
    assert installed.status_code == 200, installed.text
    role = await client.post(
        "/api/users/roles",
        headers=admin,
        json={
            "user_role_name": "Restricted UI",
            "permissions": [],
            "policies": [{"name": "allowed_plugin_ui", "value": json.dumps([plugin])}],
        },
    )
    assert role.status_code == 201, role.text
    rid = role.json()["user_role_id"]
    user = await client.post(
        "/api/users",
        headers=admin,
        json={
            "username": "restricted",
            "password": TEST_PASSWORD,
            "role": rid,
        },
    )
    assert user.status_code == 201, user.text
    auth = bearer(await login(client, username="restricted"))
    path = f"/api/plugins/{plugin}/ui/plugin.yaml"
    allowed = await client.get(path, headers=auth)
    assert allowed.status_code == 200
    changed = await client.patch(
        f"/api/users/roles/{rid}",
        headers=admin,
        json={
            "policies": [{"name": "allowed_plugin_ui", "value": "[]"}],
        },
    )
    assert changed.status_code == 200, changed.text
    for extra in ({}, {"If-None-Match": allowed.headers["etag"]}, {"Range": "bytes=0-10"}):
        denied = await client.get(path, headers={**auth, **extra})
        assert denied.status_code == 403, denied.text
    assert (await client.get("/api/plugins", headers=auth)).json() == []
    assert (await client.get("/api/plugins/market", headers=auth)).json() == []
    assert (
        await client.get(f"/api/plugins/market/{plugin}/ui/icon.png", headers=auth)
    ).status_code == 403
    assert (await client.get(path, headers=admin)).status_code == 200

    # An old dashboard's full-array save cannot erase the security grant.
    old_ui_save = await client.patch(
        f"/api/users/roles/{rid}", headers=admin, json={"policies": []}
    )
    assert old_ui_save.status_code == 200
    assert (await client.get(path, headers=auth)).status_code == 403
