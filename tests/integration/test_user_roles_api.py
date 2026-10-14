"""Role templates supply defaults; users.role stores the template public id."""

from __future__ import annotations

from tests.support.auth import TEST_PASSWORD, bearer, login


async def test_builtin_roles_invite_reads_role_at_redeem(env):
    c, _srv, auth = env
    listed = await c.get("/api/users/roles", headers=auth)
    assert listed.status_code == 200, listed.text
    by_id = {row["user_role_id"]: row for row in listed.json()}
    assert by_id["admin"]["deletable"] is False
    assert by_id["admin"]["immutable"] is True
    assert by_id["admin"]["policies"] == []
    assert by_id["admin"]["system_role"] == "admin"
    assert by_id["user"]["deletable"] is False
    assert "channels" in by_id["user"]["permissions"]
    assert by_id["user"]["policies"] == []

    denied = await c.delete("/api/users/roles/admin", headers=auth)
    assert denied.status_code == 403
    denied_user = await c.delete("/api/users/roles/user", headers=auth)
    assert denied_user.status_code == 403

    created = await c.post(
        "/api/users/roles",
        headers=auth,
        json={
            "user_role_name": "分析师",
            "description": "只读分析工作",
            "permissions": ["browser"],
            "policies": [{"name": "token_quota", "value": "1000"}],
        },
    )
    assert created.status_code == 201, created.text
    role = created.json()
    assert role["description"] == "只读分析工作"
    assert role["policies"] == [{"name": "token_quota", "value": "1000"}]
    assert role["workspace_root_dir"] is None
    assert role["max_agents"] is None

    user = await c.post(
        "/api/users",
        headers=auth,
        json={
            "username": "carol",
            "password": "TestPass12",
            "role": role["user_role_id"],
            "permissions": ["browser"],
            "role_name": "分析师",
            "token_quota": 1000,
        },
    )
    assert user.status_code == 201, user.text
    assert user.json()["role"] == role["user_role_id"]
    assert user.json()["role_name"] == "分析师"
    assert "user_role_id" not in user.json()
    uid = user.json()["id"]

    renamed = await c.patch(
        f"/api/users/roles/{role['user_role_id']}",
        headers=auth,
        json={
            "user_role_name": "分析",
            "permissions": ["desktop"],
            "policies": [{"name": "token_quota", "value": "5"}],
        },
    )
    assert renamed.status_code == 200, renamed.text
    kept = await c.get(f"/api/users/{uid}", headers=auth)
    assert kept.json()["role_name"] == "分析师"
    assert kept.json()["permissions"] == ["browser"]
    assert kept.json()["token_quota"] == 1000

    # Still assigned — delete must be refused.
    blocked = await c.delete(f"/api/users/roles/{role['user_role_id']}", headers=auth)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "USER_ROLE_IN_USE"

    # Switch to built-in user (clears template policies), then delete template.
    reassigned = await c.patch(
        f"/api/users/{uid}",
        headers=auth,
        json={"role": "user"},
    )
    assert reassigned.status_code == 200, reassigned.text
    assert reassigned.json()["role"] == "user"
    assert reassigned.json()["token_quota"] is None

    deleted = await c.delete(f"/api/users/roles/{role['user_role_id']}", headers=auth)
    assert deleted.status_code == 204

    invite = await c.post(
        "/api/users/invites",
        headers=auth,
        json={"role": "user", "note": "later"},
    )
    assert invite.status_code == 201, invite.text
    assert invite.json()["role_name"] == "用户"
    assert invite.json()["role"] == "user"
    assert "user_role_id" not in invite.json()
    assert "system_role" not in invite.json()

    patched = await c.patch(
        "/api/users/roles/user",
        headers=auth,
        json={"user_role_name": "普通用户", "permissions": ["browser"]},
    )
    assert patched.status_code == 200, patched.text

    redeem = await c.post(
        "/api/auth/invite/redeem",
        json={
            "code": invite.json()["code"],
            "username": "invited_one",
            "password": "InvitePass12",
        },
    )
    assert redeem.status_code == 200, redeem.text
    assert redeem.json()["user"]["role"] == "user"
    assert redeem.json()["user"]["permissions"] == ["browser"]

    invited = await c.get("/api/users", headers=auth)
    row = next(item for item in invited.json() if item["username"] == "invited_one")
    assert row["role"] == "user"
    assert row["role_name"] == "普通用户"
    assert row["permissions"] == ["browser"]

    me = await c.get("/api/auth/me", headers=auth)
    admin_id = me.json()["id"]
    admin_row = next(item for item in invited.json() if item["id"] == admin_id)
    assert admin_row["role"] == "admin"
    assert admin_row["role_name"] == "管理员"


async def test_role_patch_without_policies_keeps_existing_policies(env):
    c, _srv, auth = env
    created = await c.post(
        "/api/users/roles",
        headers=auth,
        json={
            "user_role_name": "限额",
            "permissions": ["browser"],
            "policies": [
                {"name": "token_quota", "value": "1000"},
                {"name": "max_agents", "value": "3"},
            ],
        },
    )
    assert created.status_code == 201, created.text
    role_id = created.json()["user_role_id"]

    patched = await c.patch(
        f"/api/users/roles/{role_id}",
        headers=auth,
        json={"permissions": ["desktop"]},
    )
    assert patched.status_code == 200, patched.text
    names = {item["name"]: item["value"] for item in patched.json()["policies"]}
    assert names == {"token_quota": "1000", "max_agents": "3"}


async def test_redeem_fails_when_named_role_is_gone(env):
    c, _srv, auth = env
    created = await c.post(
        "/api/users/roles",
        headers=auth,
        json={"user_role_name": "临时", "permissions": ["browser"]},
    )
    assert created.status_code == 201, created.text
    role_id = created.json()["user_role_id"]
    invite = await c.post(
        "/api/users/invites",
        headers=auth,
        json={"role": role_id},
    )
    assert invite.status_code == 201, invite.text
    deleted = await c.delete(f"/api/users/roles/{role_id}", headers=auth)
    assert deleted.status_code == 204, deleted.text

    redeem = await c.post(
        "/api/auth/invite/redeem",
        json={
            "code": invite.json()["code"],
            "username": "missing_role",
            "password": "InvitePass12",
        },
    )
    assert redeem.status_code == 409, redeem.text
    assert redeem.json()["error"]["code"] == "INVITE_ROLE_MISSING"
    listed = await c.get("/api/users", headers=auth)
    assert all(item["username"] != "missing_role" for item in listed.json())


async def test_non_admin_cannot_grant_administrator(env):
    c, _srv, auth = env
    created = await c.post(
        "/api/users",
        headers=auth,
        json={
            "username": "clerk",
            "password": TEST_PASSWORD,
            "role": "user",
            "permissions": ["users"],
        },
    )
    assert created.status_code == 201, created.text
    clerk = bearer(await login(c, username="clerk", password=TEST_PASSWORD))

    denied_user = await c.post(
        "/api/users",
        headers=clerk,
        json={"username": "boss", "password": TEST_PASSWORD, "role": "admin"},
    )
    assert denied_user.status_code == 403, denied_user.text

    denied_invite = await c.post(
        "/api/users/invites",
        headers=clerk,
        json={"role": "admin"},
    )
    assert denied_invite.status_code == 403, denied_invite.text

    target = created.json()["id"]
    denied_patch = await c.patch(
        f"/api/users/{target}",
        headers=clerk,
        json={"role": "admin"},
    )
    assert denied_patch.status_code == 403, denied_patch.text


async def test_role_visible_nav_limits_its_accounts(env):
    c, _srv, auth = env
    created = await c.post(
        "/api/users/roles",
        headers=auth,
        json={
            "user_role_name": "对话成员",
            "permissions": ["knowledge_bases"],
            "policies": [{"name": "visible_nav", "value": "knowledge-bases"}],
        },
    )
    assert created.status_code == 201, created.text
    role_id = created.json()["user_role_id"]
    # Chat is always kept, since that is where login lands.
    assert created.json()["visible_nav"] == ["chat", "knowledge-bases"]

    unknown = await c.patch(
        f"/api/users/roles/{role_id}",
        headers=auth,
        json={"policies": [{"name": "visible_nav", "value": "chat,nope"}]},
    )
    assert unknown.status_code == 400, unknown.text

    user = await c.post(
        "/api/users",
        headers=auth,
        json={"username": "dora", "password": TEST_PASSWORD, "role": role_id},
    )
    assert user.status_code == 201, user.text
    dora = bearer(await login(c, username="dora", password=TEST_PASSWORD))

    me = await c.get("/api/auth/me", headers=dora)
    assert me.json()["visible_nav"] == ["chat", "knowledge-bases"]
    # The Experts page is hidden, so the account starts with the default expert.
    agents = await c.get("/api/agents", headers=dora)
    assert len(agents.json()) == 1, agents.text

    admin_me = await c.get("/api/auth/me", headers=auth)
    assert admin_me.json()["visible_nav"] is None

    # The role is read on each request, so one edit reaches every account in it.
    opened = await c.patch(f"/api/users/roles/{role_id}", headers=auth, json={"policies": []})
    assert opened.status_code == 200, opened.text
    assert opened.json()["visible_nav"] is None
    me = await c.get("/api/auth/me", headers=dora)
    assert me.json()["visible_nav"] is None


async def test_role_hidden_expert_tabs_reach_its_accounts(env):
    c, _srv, auth = env
    created = await c.post(
        "/api/users/roles",
        headers=auth,
        json={
            "user_role_name": "选剧成员",
            "policies": [{"name": "hidden_expert_tabs", "value": "teams,library,market"}],
        },
    )
    assert created.status_code == 201, created.text
    role_id = created.json()["user_role_id"]
    assert created.json()["hidden_expert_tabs"] == ["teams", "library", "market"]

    unknown = await c.patch(
        f"/api/users/roles/{role_id}",
        headers=auth,
        json={"policies": [{"name": "hidden_expert_tabs", "value": "library,my"}]},
    )
    assert unknown.status_code == 400, unknown.text

    user = await c.post(
        "/api/users",
        headers=auth,
        json={"username": "erin", "password": TEST_PASSWORD, "role": role_id},
    )
    assert user.status_code == 201, user.text
    erin = bearer(await login(c, username="erin", password=TEST_PASSWORD))

    me = await c.get("/api/auth/me", headers=erin)
    assert me.json()["hidden_expert_tabs"] == ["teams", "library", "market"]
    # Hidden in the dashboard only: the templates behind the tabs still answer.
    library = await c.get("/api/experts", headers=erin)
    assert library.status_code == 200, library.text

    admin_me = await c.get("/api/auth/me", headers=auth)
    assert admin_me.json()["hidden_expert_tabs"] == []

    # The role is read on each request, so one edit reaches every account in it.
    opened = await c.patch(f"/api/users/roles/{role_id}", headers=auth, json={"policies": []})
    assert opened.status_code == 200, opened.text
    assert opened.json()["hidden_expert_tabs"] == []
    me = await c.get("/api/auth/me", headers=erin)
    assert me.json()["hidden_expert_tabs"] == []


async def test_role_default_published_expert_reaches_its_accounts(env):
    c, srv, auth = env
    admin_id = (await c.get("/api/auth/me", headers=auth)).json()["id"]
    shared = srv.services.knowledge_repo.create_base(
        owner_user_id=admin_id, name="Shared", shared=True
    )
    source = await c.post(
        "/api/agents/from-expert/default", headers=auth, json={"name": "pick-source"}
    )
    assert source.status_code == 201, source.text
    published = await c.post(
        f"/api/agents/{source.json()['agent_id']}/publish-expert",
        headers=auth,
        json={"name": "选剧专家", "description": "找剧"},
    )
    assert published.status_code == 201, published.text
    expert_id = published.json()["id"]

    async def from_template(headers: dict[str, str]) -> list[dict]:
        agents = await c.get("/api/agents", headers=headers)
        assert agents.status_code == 200, agents.text
        return [row for row in agents.json() if row["published_expert_id"] == expert_id]

    async def account(username: str, role: str) -> tuple[int, dict[str, str]]:
        created = await c.post(
            "/api/users",
            headers=auth,
            json={"username": username, "password": TEST_PASSWORD, "role": role},
        )
        assert created.status_code == 201, created.text
        token = await login(c, username=username, password=TEST_PASSWORD)
        return created.json()["id"], bearer(token)

    base_policy = {"name": "default_knowledge_bases", "value": shared.id}
    role = await c.post(
        "/api/users/roles",
        headers=auth,
        json={
            "user_role_name": "选剧新人",
            "permissions": ["knowledge_bases"],
            "policies": [base_policy],
        },
    )
    assert role.status_code == 201, role.text
    role_id = role.json()["user_role_id"]
    assert role.json()["default_published_expert"] is None
    _, fern = await account("fern", role_id)
    assert await from_template(fern) == []

    # Naming a template also reaches the accounts already in the role.
    policies = [base_policy, {"name": "default_published_expert", "value": expert_id}]
    named = await c.patch(f"/api/users/roles/{role_id}", headers=auth, json={"policies": policies})
    assert named.status_code == 200, named.text
    assert named.json()["default_published_expert"] == expert_id
    assert named.json()["default_expert_installed"] == 1
    # The account's profile names the template, so the dashboard can open on that expert.
    fern_me = await c.get("/api/auth/me", headers=fern)
    assert fern_me.json()["default_published_expert"] == expert_id
    admin_me = await c.get("/api/auth/me", headers=auth)
    assert admin_me.json()["default_published_expert"] is None
    installed = await from_template(fern)
    assert [row["name"] for row in installed] == ["选剧专家"]
    # It carries the role's knowledge bases, like the default expert does.
    assert installed[0]["knowledge_base_ids"] == [shared.id]

    # A new account gets it when it is created.
    _, gus = await account("gus", role_id)
    assert len(await from_template(gus)) == 1

    # An invited account gets it next to the default assistant.
    invite = await c.post("/api/users/invites", headers=auth, json={"role": role_id})
    assert invite.status_code == 201, invite.text
    redeem = await c.post(
        "/api/auth/invite/redeem",
        json={"code": invite.json()["code"], "username": "ivy", "password": TEST_PASSWORD},
    )
    assert redeem.status_code == 200, redeem.text
    ivy = bearer(redeem.json()["access_token"])
    ivy_agents = (await c.get("/api/agents", headers=ivy)).json()
    assert sorted(str(row["template_name"]) for row in ivy_agents) == ["None", "general-assistant"]
    assert len(await from_template(ivy)) == 1

    # So does an account moved into the role.
    hal_id, hal = await account("hal", "user")
    assert await from_template(hal) == []
    moved = await c.patch(f"/api/users/{hal_id}", headers=auth, json={"role": role_id})
    assert moved.status_code == 200, moved.text
    assert len(await from_template(hal)) == 1

    # Saving the role again leaves an account that removed its copy alone.
    removed = await c.delete(f"/api/agents/{installed[0]['agent_id']}", headers=fern)
    assert removed.status_code == 204, removed.text
    again = await c.patch(
        f"/api/users/roles/{role_id}",
        headers=auth,
        json={"description": "选剧用", "policies": policies},
    )
    assert again.status_code == 200, again.text
    assert "default_expert_installed" not in again.json()
    assert await from_template(fern) == []
    assert len(await from_template(gus)) == 1


async def test_role_default_knowledge_bases_reach_the_first_expert(env):
    c, srv, auth = env
    admin_id = (await c.get("/api/auth/me", headers=auth)).json()["id"]
    repo = srv.services.knowledge_repo
    shared = repo.create_base(owner_user_id=admin_id, name="Shared", shared=True)
    private = repo.create_base(owner_user_id=admin_id, name="Private", shared=False)

    created = await c.post(
        "/api/users/roles",
        headers=auth,
        json={
            "user_role_name": "选剧成员",
            "permissions": ["knowledge_bases"],
            "policies": [
                {"name": "visible_nav", "value": "chat"},
                # A base the account cannot see, or one deleted since, is left out.
                {"name": "default_knowledge_bases", "value": f"{shared.id},{private.id},gone"},
            ],
        },
    )
    assert created.status_code == 201, created.text
    role = created.json()
    assert role["default_knowledge_bases"] == [shared.id, private.id, "gone"]

    user = await c.post(
        "/api/users",
        headers=auth,
        json={"username": "erin", "password": TEST_PASSWORD, "role": role["user_role_id"]},
    )
    assert user.status_code == 201, user.text
    erin = bearer(await login(c, username="erin", password=TEST_PASSWORD))
    agents = (await c.get("/api/agents", headers=erin)).json()
    assert [agent["knowledge_base_ids"] for agent in agents] == [[shared.id]]

    invite = await c.post("/api/users/invites", headers=auth, json={"role": role["user_role_id"]})
    assert invite.status_code == 201, invite.text
    redeem = await c.post(
        "/api/auth/invite/redeem",
        json={"code": invite.json()["code"], "username": "finn", "password": TEST_PASSWORD},
    )
    assert redeem.status_code == 200, redeem.text
    finn = bearer(redeem.json()["access_token"])
    agents = (await c.get("/api/agents", headers=finn)).json()
    assert [agent["knowledge_base_ids"] for agent in agents] == [[shared.id]]

    listed = await c.get("/api/users/roles", headers=auth)
    by_id = {item["user_role_id"]: item for item in listed.json()}
    assert by_id["admin"]["default_knowledge_bases"] == []


_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


async def test_user_and_role_avatars_roundtrip(env):
    c, _srv, auth = env
    me = await c.get("/api/auth/me", headers=auth)
    user_id = me.json()["id"]
    uploaded = await c.post(
        f"/api/users/{user_id}/avatar",
        headers=auth,
        files={"file": ("avatar.png", _PNG, "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    avatar_url = uploaded.json()["avatar_url"]
    assert avatar_url.startswith(f"/api/users/{user_id}/avatar?v=")
    fetched = await c.get(f"/api/users/{user_id}/avatar", headers=auth)
    assert fetched.status_code == 200
    assert fetched.headers["content-type"].startswith("image/png")
    assert fetched.content == _PNG

    role = await c.post(
        "/api/users/roles",
        headers=auth,
        json={"user_role_name": "带头像", "permissions": ["browser"]},
    )
    assert role.status_code == 201, role.text
    role_id = role.json()["user_role_id"]
    role_upload = await c.post(
        f"/api/users/roles/{role_id}/avatar",
        headers=auth,
        files={"file": ("avatar.png", _PNG, "image/png")},
    )
    assert role_upload.status_code == 201, role_upload.text
    listed = await c.get("/api/users/roles", headers=auth)
    saved = next(item for item in listed.json() if item["user_role_id"] == role_id)
    assert saved["avatar_url"].startswith(f"/api/users/roles/{role_id}/avatar?v=")

    chosen = await c.patch(
        f"/api/users/roles/{role_id}",
        headers=auth,
        json={"avatar_icon": "admin"},
    )
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["avatar_icon"] == "admin"
    assert chosen.json()["avatar_url"] is None
    cleared = await c.get(f"/api/users/roles/{role_id}/avatar", headers=auth)
    assert cleared.status_code == 404

    user_icon = await c.patch(
        f"/api/users/{user_id}",
        headers=auth,
        json={"avatar_icon": "curly"},
    )
    assert user_icon.status_code == 200, user_icon.text
    assert user_icon.json()["avatar_icon"] == "curly"
    assert user_icon.json()["avatar_url"] is None
    invalid = await c.patch(
        f"/api/users/{user_id}",
        headers=auth,
        json={"avatar_icon": "Not A Icon"},
    )
    assert invalid.status_code == 400

    removed = await c.delete(f"/api/users/roles/{role_id}", headers=auth)
    assert removed.status_code == 204
    gone = await c.get(f"/api/users/roles/{role_id}/avatar", headers=auth)
    assert gone.status_code == 404
