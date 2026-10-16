"""Unit tests for applying ChatGPT (Codex) OAuth credentials to the provider store."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from octop.api.routers.providers import _run_codex_device_poll
from octop.config import OctopConfig
from octop.infra.agents.providers.codex_apply import (
    CODEX_FALLBACK_MODELS,
    CODEX_PROVIDER_NAME,
    apply_codex_credentials,
)
from octop.infra.agents.providers.codex_oauth import CodexOAuthCredentials
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import SharedServices, build_shared_services
from octop.infra.utils.paths import PathLayout

CATALOG_MODELS = [
    {"id": "gpt-6.1-sol", "name": "GPT-6.1-Sol", "enabled": True, "input": ["text", "image"]},
    {"id": "gpt-6-astra", "name": "GPT-6-Astra", "enabled": True, "input": ["text", "image"]},
    {"id": "gpt-6-luna", "name": "GPT-6-Luna", "enabled": True, "input": ["text", "image"]},
]


@pytest.fixture
def services(tmp_path: Path) -> SharedServices:
    paths = PathLayout(tmp_path / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    return build_shared_services(db=db, paths=paths, config=OctopConfig())


def _cred() -> CodexOAuthCredentials:
    return CodexOAuthCredentials(
        access="access-tok", refresh="refresh-tok", expires=0, account_id="acct-123"
    )


def _add_deepseek(services: SharedServices) -> None:
    services.provider_repo.create(
        name="deepseek",
        kind="openai",
        base_url="https://api.deepseek.com",
        api_key="sk-test",
        models_json=json.dumps([{"id": "deepseek-chat", "enabled": True}]),
    )


def _codex_model_ids(services: SharedServices) -> list[str]:
    row = services.provider_repo.get_by_name(CODEX_PROVIDER_NAME)
    assert row is not None
    return [m["id"] for m in row.get_models()]


def test_apply_stores_catalog_models_with_preferred_model_first(
    services: SharedServices,
) -> None:
    apply_codex_credentials(services, services.paths, _cred(), models=CATALOG_MODELS)

    assert _codex_model_ids(services) == ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"]


def test_apply_without_catalog_uses_fallback_models(services: SharedServices) -> None:
    apply_codex_credentials(services, services.paths, _cred(), models=None)

    assert _codex_model_ids(services) == [m["id"] for m in CODEX_FALLBACK_MODELS]


def test_apply_keeps_existing_active_model(services: SharedServices) -> None:
    _add_deepseek(services)
    services.settings_repo.set_active_model("deepseek", "deepseek-chat")

    apply_codex_credentials(services, services.paths, _cred(), models=CATALOG_MODELS)

    assert services.settings_repo.get_active_model() == ("deepseek", "deepseek-chat")


def test_apply_leaves_empty_active_model_when_another_provider_is_usable(
    services: SharedServices,
) -> None:
    # Cloud setup on 2026-10-16: only DeepSeek, no global default; agents fall back to it.
    _add_deepseek(services)

    apply_codex_credentials(services, services.paths, _cred(), models=CATALOG_MODELS)

    assert services.settings_repo.get_active_model() == ("", "")


def test_apply_sets_active_model_when_nothing_else_is_usable(services: SharedServices) -> None:
    apply_codex_credentials(services, services.paths, _cred(), models=CATALOG_MODELS)

    assert services.settings_repo.get_active_model() == (CODEX_PROVIDER_NAME, "gpt-6-astra")


def test_relogin_replaces_stale_codex_default_when_nothing_else_is_usable(
    services: SharedServices,
) -> None:
    services.provider_repo.create(
        name=CODEX_PROVIDER_NAME,
        kind="openai",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="old-tok",
        models_json=json.dumps([{"id": "gpt-5.4", "enabled": True}]),
    )
    services.settings_repo.set_active_model(CODEX_PROVIDER_NAME, "gpt-5.4")

    apply_codex_credentials(services, services.paths, _cred(), models=CATALOG_MODELS)

    assert services.settings_repo.get_active_model() == (CODEX_PROVIDER_NAME, "gpt-6-astra")
    assert _codex_model_ids(services)[0] == "gpt-6-astra"


async def test_device_poll_applies_fetched_catalog_without_touching_default(
    services: SharedServices,
) -> None:
    _add_deepseek(services)
    server: Any = SimpleNamespace(services=services, app_runtime=None)

    with (
        mock.patch(
            "octop.api.routers.providers.poll_device_token", return_value=("code", "verifier")
        ),
        mock.patch("octop.api.routers.providers.exchange_device_code", return_value=_cred()),
        mock.patch(
            "octop.api.routers.providers.fetch_codex_models", return_value=CATALOG_MODELS
        ) as fetch,
    ):
        await _run_codex_device_poll(
            server,
            state_id="state-1",
            device_auth_id="dev-1",
            user_code="ABCD-1234",
            interval_s=0,
            user_id=1,
        )

    fetch.assert_called_once_with("access-tok", "acct-123")
    pending = json.loads(services.settings_repo.get("codex_oauth.pending.state-1") or "{}")
    assert pending["status"] == "ok"
    assert _codex_model_ids(services) == ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"]
    assert services.settings_repo.get_active_model() == ("", "")
