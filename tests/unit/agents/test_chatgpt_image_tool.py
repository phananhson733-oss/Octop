"""Unit tests for the ``chatgpt_image`` host tool and its settings."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from octop.infra.agents.manager import AgentManager
from octop.infra.agents.providers import codex_image_tool
from octop.infra.agents.providers.codex_image import USAGE_URL
from octop.infra.agents.providers.codex_image_tool import (
    build_chatgpt_image_tools,
    read_quota_status,
)
from octop.infra.agents.providers.codex_oauth import CodexOAuthCredentials, save_codex_token
from octop.infra.agents.settings.chatgpt_image import (
    ChatgptImageSettings,
    ChatgptImageSettingsStore,
)
from octop.infra.agents.settings.tool_catalog import builtin_tool_available
from octop.infra.gateway.media.tool_media import is_media_push_tool
from octop.infra.users.identity import Role
from octop.infra.utils.paths import PathLayout

USAGE_OK = {
    "plan_type": "pro",
    "rate_limit": {"primary_window": {"used_percent": 40, "reset_at": 1792649637}},
    "credits": {"has_credits": True, "unlimited": False, "balance": "10"},
}
USAGE_EXHAUSTED = {
    "plan_type": "pro",
    "rate_limit": {"primary_window": {"used_percent": 100, "reset_at": 1792649637}},
    "credits": {"has_credits": True, "unlimited": False, "balance": "10"},
}


def _png() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR"
        + (1024).to_bytes(4, "big")
        + (1024).to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00\x00\x00\x00\x00"
    )


def _image_stream() -> bytes:
    event = {
        "type": "response.output_item.done",
        "item": {"type": "image_generation_call", "result": base64.b64encode(_png()).decode()},
    }
    return f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n".encode()


class FakeSettingsRepo:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value


class FakeProviderRepo:
    def __init__(self, api_key: str = "tok-old") -> None:
        self.row = SimpleNamespace(
            id=7,
            api_key=api_key,
            get_models=lambda: [
                {"id": "gpt-5.4", "enabled": True},
                {"id": "gpt-6-luna", "enabled": False},
            ],
        )
        self.updates: list[tuple[int, dict[str, Any]]] = []

    def get_by_name(self, name: str) -> Any:
        return self.row if name == "openai-codex" else None

    def update(self, pid: int, **fields: Any) -> None:
        self.updates.append((pid, fields))


class FakeWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.files: dict[str, bytes] = {}

    async def aupload_bytes(self, path: str, data: bytes) -> None:
        self.files[path] = data

    def materialize_local(self, path: str) -> Path:
        return self.root / path


@pytest.fixture
def paths(tmp_path: Path) -> PathLayout:
    layout = PathLayout(tmp_path)
    save_codex_token(
        layout,
        CodexOAuthCredentials(
            access="tok-new",
            refresh="refresh",
            expires=int(time.time() * 1000) + 3_600_000,
            account_id="acct-1",
        ),
    )
    return layout


def _store(**settings: bool) -> ChatgptImageSettingsStore:
    store = ChatgptImageSettingsStore(FakeSettingsRepo())  # type: ignore[arg-type]
    store.save(ChatgptImageSettings(**settings))
    return store


def _mock_http(monkeypatch: pytest.MonkeyPatch, usage: dict[str, Any] | int) -> list[str]:
    calls: list[str] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == USAGE_URL:
            if isinstance(usage, int):
                return httpx.Response(usage, text="error")
            return httpx.Response(200, json=usage)
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(200, content=_image_stream())

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    return calls


def _tool(paths: PathLayout, store: ChatgptImageSettingsStore, workspace: FakeWorkspace, repo: Any):
    return build_chatgpt_image_tools(
        workspace=workspace,  # type: ignore[arg-type]
        paths=paths,
        provider_repo=repo,
        settings_store=store,
    )[0]


@pytest.fixture(autouse=True)
def _admin_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_image_tool, "_acting_user_is_admin", lambda: True)


async def test_generates_and_saves_image_to_workspace(
    paths: PathLayout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _mock_http(monkeypatch, USAGE_OK)
    workspace = FakeWorkspace(tmp_path)
    repo = FakeProviderRepo()

    result = await _tool(paths, _store(enabled=True), workspace, repo).ainvoke(
        {"prompt": " a lighthouse ", "quality": "low"}
    )

    assert result["type"] == "image_gen_tool_result"
    assert result["status"] == "completed"
    assert result["prompt"] == "a lighthouse"
    image = result["images"][0]
    assert image["path"].startswith("generated/image/chatgpt-")
    assert image["path"].endswith(".png")
    assert image["mediaType"] == "image/png"
    assert workspace.files[image["path"]] == _png()
    assert result["execution"]["host_model"] == "gpt-6-astra"
    assert result["execution"]["size"] == "1024x1024"
    assert calls[0] == USAGE_URL
    assert repo.updates == [(7, {"api_key": "tok-new"})]


async def test_exhausted_allowance_stops_before_generation(
    paths: PathLayout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _mock_http(monkeypatch, USAGE_EXHAUSTED)
    workspace = FakeWorkspace(tmp_path)

    result = await _tool(paths, _store(enabled=True), workspace, FakeProviderRepo()).ainvoke(
        {"prompt": "x"}
    )

    assert result["status"] == "failed"
    assert result["error"]["code"] == "quota_exhausted"
    assert result["error"]["safe_to_resubmit"] is True
    assert "2026-10-22 06:13 UTC" in result["message"]
    assert calls == [USAGE_URL]
    assert workspace.files == {}


async def test_unreadable_allowance_fails_closed(
    paths: PathLayout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _mock_http(monkeypatch, 503)

    result = await _tool(
        paths, _store(enabled=True), FakeWorkspace(tmp_path), FakeProviderRepo()
    ).ainvoke({"prompt": "x"})

    assert result["error"]["code"] == "quota_unknown"
    assert calls == [USAGE_URL]


async def test_allow_credits_skips_the_allowance_check(
    paths: PathLayout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _mock_http(monkeypatch, USAGE_EXHAUSTED)

    result = await _tool(
        paths, _store(enabled=True, allow_credits=True), FakeWorkspace(tmp_path), FakeProviderRepo()
    ).ainvoke({"prompt": "x"})

    assert result["status"] == "completed"
    assert USAGE_URL not in calls


async def test_disabled_and_non_admin_and_logged_out_send_nothing(
    paths: PathLayout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _mock_http(monkeypatch, USAGE_OK)
    workspace = FakeWorkspace(tmp_path)

    disabled = await _tool(paths, _store(), workspace, FakeProviderRepo()).ainvoke({"prompt": "x"})
    monkeypatch.setattr(codex_image_tool, "_acting_user_is_admin", lambda: False)
    non_admin = await _tool(paths, _store(enabled=True), workspace, FakeProviderRepo()).ainvoke(
        {"prompt": "x"}
    )
    opened_up = await _tool(
        paths, _store(enabled=True, admin_only=False), workspace, FakeProviderRepo()
    ).ainvoke({"prompt": "x"})
    logged_out = await _tool(
        PathLayout(tmp_path / "empty"),
        _store(enabled=True, admin_only=False),
        workspace,
        FakeProviderRepo(),
    ).ainvoke({"prompt": "x"})

    assert disabled["error"]["code"] == "disabled"
    assert non_admin["error"]["code"] == "admin_only"
    assert opened_up["status"] == "completed"
    assert logged_out["error"]["code"] == "auth_required"
    assert calls.count(USAGE_URL) == 1


async def test_quota_status_without_login_makes_no_request(tmp_path: Path) -> None:
    status = await read_quota_status(PathLayout(tmp_path), FakeProviderRepo())

    assert status.logged_in is False
    assert status.quota is None


def test_settings_default_to_off_admin_only_no_credits() -> None:
    repo = FakeSettingsRepo()
    store = ChatgptImageSettingsStore(repo)  # type: ignore[arg-type]

    assert store.load() == ChatgptImageSettings(enabled=False, admin_only=True, allow_credits=False)
    repo.data["chatgpt_image_config"] = "not json"
    assert store.load() == ChatgptImageSettings()
    store.save(ChatgptImageSettings(enabled=True, admin_only=False, allow_credits=True))
    assert store.load() == ChatgptImageSettings(enabled=True, admin_only=False, allow_credits=True)


def _mount(settings: ChatgptImageSettings, *, role: Role | None, team: bool = False) -> bool:
    store = ChatgptImageSettingsStore(FakeSettingsRepo())  # type: ignore[arg-type]
    store.save(settings)
    owner = SimpleNamespace(role=role) if role is not None else None
    manager = SimpleNamespace(
        _chatgpt_image=store,
        _repos=SimpleNamespace(user_repo=SimpleNamespace(get=lambda _uid: owner)),
    )
    row = SimpleNamespace(
        user_id=1, agent_id="a1", kind="team" if team else "agent", config_json="{}"
    )
    return AgentManager.chatgpt_image_mounted(manager, row)  # type: ignore[arg-type]


def test_tool_mounts_only_for_admin_owned_agents_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("octop.infra.agents.teams.is_team_agent", lambda row: row.kind == "team")
    on = ChatgptImageSettings(enabled=True)

    assert _mount(on, role=Role.ADMIN) is True
    assert _mount(on, role=Role.USER) is False
    assert _mount(on, role=None) is False
    assert _mount(ChatgptImageSettings(), role=Role.ADMIN) is False
    assert _mount(ChatgptImageSettings(enabled=True, admin_only=False), role=Role.USER) is True
    assert _mount(on, role=Role.ADMIN, team=True) is False


def test_tool_is_a_media_tool() -> None:
    assert is_media_push_tool("chatgpt_image") is True
    assert builtin_tool_available("chatgpt_image", agent_cfg={}) is False
    assert builtin_tool_available("chatgpt_image", agent_cfg={}, chatgpt_image_mounted=True) is True


def _route_server(
    paths: PathLayout, store: ChatgptImageSettingsStore, workspace: FakeWorkspace
) -> SimpleNamespace:
    row = SimpleNamespace(agent_id="a1", user_id=1, is_shared=0, enabled=True, last_state="running")
    registry = SimpleNamespace(
        get_row=lambda _aid: row,
        get_agent=lambda _aid: SimpleNamespace(workspace=workspace),
        chatgpt_image=store,
    )
    return SimpleNamespace(
        app_runtime=SimpleNamespace(agent_registry=registry),
        services=SimpleNamespace(paths=paths, provider_repo=FakeProviderRepo()),
    )


async def test_dashboard_route_uses_the_caller_not_the_agent_context(
    paths: PathLayout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from octop.api.routers.workspace import ChatgptImageBody, generate_chatgpt_image
    from octop.infra.errors import OctopError

    # The route must not read the LangGraph config: it gets the caller from the request.
    monkeypatch.setattr(codex_image_tool, "_acting_user_is_admin", lambda: pytest.fail("tool path"))
    calls = _mock_http(monkeypatch, USAGE_OK)
    workspace = FakeWorkspace(tmp_path)
    server = _route_server(paths, _store(enabled=True), workspace)
    body = ChatgptImageBody(prompt="a brass plumb bob", aspect_ratio="landscape", quality="low")

    owner = SimpleNamespace(id=1, is_admin=False)
    refused = await generate_chatgpt_image("a1", body, user=owner, server=server)
    admin = SimpleNamespace(id=9, is_admin=True)
    done = await generate_chatgpt_image("a1", body, user=admin, server=server)
    with pytest.raises(OctopError):
        await generate_chatgpt_image(
            "a1", body, user=SimpleNamespace(id=2, is_admin=False), server=server
        )

    assert refused.status == "failed"
    assert refused.error is not None and refused.error.code == "admin_only"
    assert refused.error.safe_to_resubmit is True
    assert done.status == "completed" and done.error is None
    assert done.images[0].path.startswith("generated/image/chatgpt-")
    assert workspace.files[done.images[0].path] == _png()
    assert done.execution is not None and done.execution["requested_size"] == "1536x1024"
    assert calls.count(USAGE_URL) == 1
