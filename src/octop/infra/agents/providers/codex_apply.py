"""Apply Codex OAuth credentials to the Octop provider store."""

from __future__ import annotations

import json
from typing import Any

from octop.infra.agents.providers.codex_oauth import (
    CODEX_BASE_URL,
    CodexOAuthCredentials,
    build_codex_headers,
    save_codex_token,
)
from octop.infra.agents.providers.store import ProviderStore
from octop.infra.utils.paths import PathLayout

CODEX_PROVIDER_NAME = "openai-codex"

# Verified with a ChatGPT account on 2026-10-16. Listed first whenever the account offers
# it, because the first model becomes the global default when nothing else is usable
# (the catalog's top-priority model can still be rejected for some accounts).
CODEX_PREFERRED_MODEL = "gpt-6-astra"

# Used when the account's model catalog cannot be fetched.
CODEX_FALLBACK_MODELS = [
    {"id": "gpt-6-astra", "name": "GPT-6-Astra", "enabled": True, "input": ["text", "image"]},
    {"id": "gpt-6-sol", "name": "GPT-6-Sol", "enabled": True, "input": ["text", "image"]},
    {"id": "gpt-6-luna", "name": "GPT-6-Luna", "enabled": True, "input": ["text", "image"]},
]


def _preferred_first(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    preferred = [m for m in models if m.get("id") == CODEX_PREFERRED_MODEL]
    return preferred + [m for m in models if m.get("id") != CODEX_PREFERRED_MODEL]


def _has_usable_default(services: Any) -> bool:
    """True when agents already resolve a model without relying on the Codex provider."""
    store = ProviderStore(services.provider_repo)
    name, model = services.settings_repo.get_active_model()
    if name and model and store.is_model_ref_usable(f"{name}/{model}"):
        return True
    return any(row.name != CODEX_PROVIDER_NAME for row in store.iter_usable_rows())


def apply_codex_credentials(
    services: Any,
    paths: PathLayout,
    cred: CodexOAuthCredentials,
    *,
    models: list[dict[str, Any]] | None = None,
    set_active: bool = True,
) -> int:
    """Create or update the ``openai-codex`` provider row. Returns provider id.

    *models* is the account's catalog from ``fetch_codex_models``; ``None`` falls back to
    ``CODEX_FALLBACK_MODELS``. With *set_active*, the global default model is set to the
    first Codex model only when no usable default exists — an existing default, or another
    usable provider that agents already fall back to, is left alone.
    """
    save_codex_token(paths, cred)
    account_id = cred.get("account_id", "")
    extra_json = json.dumps({"headers": build_codex_headers(account_id)})
    model_list = _preferred_first(models or CODEX_FALLBACK_MODELS)
    models_json = json.dumps(model_list)
    row = services.provider_repo.get_by_name(CODEX_PROVIDER_NAME)
    if row is None:
        pid = services.provider_repo.create(
            name=CODEX_PROVIDER_NAME,
            kind="openai",
            base_url=CODEX_BASE_URL,
            api_key=cred["access"],
            extra_json=extra_json,
            models_json=models_json,
            note="ChatGPT OAuth (Codex)",
        )
    else:
        services.provider_repo.update(
            row.id,
            kind="openai",
            base_url=CODEX_BASE_URL,
            api_key=cred["access"],
            extra_json=extra_json,
            models_json=models_json,
            enabled=True,
        )
        pid = row.id
    if set_active and not _has_usable_default(services):
        services.settings_repo.set_active_model(CODEX_PROVIDER_NAME, model_list[0]["id"])
    return int(pid)


def sync_refreshed_codex_api_key(services: Any, paths: PathLayout, access_token: str) -> None:
    row = services.provider_repo.get_by_name(CODEX_PROVIDER_NAME)
    if row is not None:
        services.provider_repo.update(row.id, api_key=access_token)
