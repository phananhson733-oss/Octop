"""``chatgpt_image``: an Octop host tool that draws on the ChatGPT subscription.

It returns the same ``image_gen_tool_result`` envelope as the harness
``generate_image`` tool, so the dashboard and IM channels show the image the
same way. Failures come back as that envelope too, with an instruction for the
model, rather than as exceptions.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any
from uuid import uuid4

import httpx
from langchain_core.tools import StructuredTool
from langgraph.config import get_config
from pydantic import Field

from octop.infra.agents.providers.codex_apply import CODEX_PROVIDER_NAME
from octop.infra.agents.providers.codex_image import (
    DEFAULT_HOST_MODEL,
    IMAGE_MODEL,
    REQUEST_TIMEOUT,
    SIZES,
    AspectRatio,
    CodexImageError,
    CodexQuota,
    Quality,
    fetch_quota,
    generate_image,
)
from octop.infra.agents.providers.codex_oauth import get_valid_access_token, load_codex_token
from octop.infra.agents.settings.chatgpt_image import ChatgptImageSettingsStore
from octop.infra.utils.paths import PathLayout

if TYPE_CHECKING:
    from octop_harness.backends.workspace import BackendWorkspace

TOOL_NAME = "chatgpt_image"
OUTPUT_DIR = "generated/image"
_RESULT_TYPE = "image_gen_tool_result"
_PROVIDER = "openai-codex"

_DESCRIPTION = (
    "Generate one PNG image from a text prompt with the ChatGPT subscription "
    "(gpt-image-2). The image is saved to the workspace and shown to the user. "
    "Each call uses the account's ChatGPT allowance, so do not call it again "
    "after a failure unless the result says it is safe."
)

# What the model should tell the user for each failure code.
_INSTRUCTIONS: dict[str, str] = {
    "disabled": (
        "Tell the user an administrator must turn on ChatGPT image generation "
        "in Settings > Media generation."
    ),
    "admin_only": "Tell the user this tool is limited to administrators on this instance.",
    "auth_required": (
        "Tell the user an administrator must sign in with ChatGPT on the Models settings page."
    ),
    "quota_exhausted": (
        "Do not call the tool again. Tell the user the ChatGPT allowance is used up, "
        "when it resets, and that this instance does not spend purchased credits."
    ),
    "quota_unknown": (
        "Do not call the tool again now. Tell the user the ChatGPT allowance could not "
        "be checked, so no image was generated."
    ),
    "invalid_request": "Ask the user for a non-empty image description.",
    "unsupported_model": (
        "Do not call the tool again. Tell the user ChatGPT refused every model this "
        "instance tried for image generation."
    ),
    "rate_limited": "Do not call the tool again right away. Tell the user ChatGPT is rate limiting.",
}
_DEFAULT_INSTRUCTION = (
    "Tell the user the image generation failed and include the error. "
    "Ask before trying again, because a failed call may still have used the allowance."
)
# Codes raised before anything was sent to the image endpoint.
_NOTHING_SUBMITTED = frozenset(
    {
        "disabled",
        "admin_only",
        "auth_required",
        "quota_exhausted",
        "quota_unknown",
        "invalid_request",
    }
)


@dataclass(frozen=True)
class _Login:
    token: str
    account_id: str
    host_models: tuple[str, ...]


def _acting_user_is_admin() -> bool:
    try:
        configurable = dict(get_config().get("configurable") or {})
    except RuntimeError:
        return False
    return configurable.get("user_is_admin") is True


def _host_models(row: Any) -> tuple[str, ...]:
    models = [DEFAULT_HOST_MODEL]
    if row is not None:
        for item in row.get_models():
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id and item.get("enabled", True) is not False:
                models.append(model_id)
    return tuple(dict.fromkeys(models))


def _load_login(paths: PathLayout, provider_repo: Any) -> _Login | None:
    """Fresh token plus host models; keeps the chat provider row's token current."""
    token = get_valid_access_token(paths)
    cred = load_codex_token(paths)
    if not token or cred is None:
        return None
    row = provider_repo.get_by_name(CODEX_PROVIDER_NAME)
    if row is not None and row.api_key != token:
        provider_repo.update(row.id, api_key=token)
    return _Login(token=token, account_id=cred["account_id"], host_models=_host_models(row))


@dataclass(frozen=True)
class QuotaStatus:
    """What the settings page shows: whether a login exists and the live allowance."""

    logged_in: bool
    quota: CodexQuota | None = None
    error: str | None = None


async def read_quota_status(paths: PathLayout, provider_repo: Any) -> QuotaStatus:
    """Read the ChatGPT allowance with the stored login; no image is generated."""
    login = await asyncio.to_thread(_load_login, paths, provider_repo)
    if login is None:
        return QuotaStatus(logged_in=False)
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            quota = await fetch_quota(client, token=login.token, account_id=login.account_id)
    except CodexImageError as exc:
        return QuotaStatus(logged_in=True, error=str(exc))
    return QuotaStatus(logged_in=True, quota=quota)


def _failure(
    code: str, message: str, *, prompt: str, http_status: int | None = None
) -> dict[str, object]:
    error: dict[str, object] = {
        "code": code,
        "message": message,
        "retryable": code == "rate_limited",
        "safe_to_resubmit": code in _NOTHING_SUBMITTED,
    }
    if http_status is not None:
        error["http_status"] = http_status
    return {
        "schema_version": 1,
        "type": _RESULT_TYPE,
        "tool": TOOL_NAME,
        "prompt": prompt,
        "status": "failed",
        "is_error": True,
        "message": message,
        "provider": _PROVIDER,
        "model": IMAGE_MODEL,
        "error": error,
        "remediation": {
            "action": code,
            "model_instruction": _INSTRUCTIONS.get(code, _DEFAULT_INSTRUCTION),
        },
        "execution": {"provider": _PROVIDER, "model": IMAGE_MODEL, "fallback_used": False},
        "images": [],
    }


def _reset_text(resets_at: int | None) -> str:
    if resets_at is None:
        return ""
    stamp = datetime.fromtimestamp(resets_at, UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"; it resets at {stamp}"


async def generate_to_workspace(
    *,
    workspace: BackendWorkspace,
    paths: PathLayout,
    provider_repo: Any,
    settings_store: ChatgptImageSettingsStore,
    prompt: str,
    aspect_ratio: AspectRatio = "square",
    quality: Quality = "medium",
    acting_user_is_admin: bool,
) -> dict[str, object]:
    """Generate one image into ``workspace`` under the same switches as the tool.

    Shared by the agent tool and the dashboard route, so both check the same
    settings and allowance and return the same ``image_gen_tool_result`` envelope.
    """
    prompt = prompt.strip()
    settings = await asyncio.to_thread(settings_store.load)
    if not settings.enabled:
        return _failure("disabled", "ChatGPT image generation is turned off.", prompt=prompt)
    if settings.admin_only and not acting_user_is_admin:
        return _failure(
            "admin_only",
            "ChatGPT image generation is limited to administrators.",
            prompt=prompt,
        )
    if not prompt:
        return _failure("invalid_request", "The image description is empty.", prompt=prompt)
    login = await asyncio.to_thread(_load_login, paths, provider_repo)
    if login is None:
        return _failure("auth_required", "No usable ChatGPT login is stored.", prompt=prompt)

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            if not settings.allow_credits:
                quota = await fetch_quota(client, token=login.token, account_id=login.account_id)
                if quota.plan_exhausted:
                    return _failure(
                        "quota_exhausted",
                        "The ChatGPT plan's Codex allowance is used up"
                        f"{_reset_text(quota.resets_at)}. "
                        "This instance does not bill images to purchased credits.",
                        prompt=prompt,
                    )
            image = await generate_image(
                client,
                token=login.token,
                account_id=login.account_id,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                quality=quality,
                host_models=login.host_models,
            )
    except CodexImageError as exc:
        return _failure(exc.code, str(exc), prompt=prompt, http_status=exc.http_status)

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = f"{OUTPUT_DIR}/chatgpt-{timestamp}-{uuid4().hex[:12]}.png"
    await workspace.aupload_bytes(path, image.data)
    local = await asyncio.to_thread(workspace.materialize_local, path)
    size = f"{image.width}x{image.height}"
    return {
        "schema_version": 1,
        "type": _RESULT_TYPE,
        "tool": TOOL_NAME,
        "prompt": prompt,
        "status": "completed",
        "is_error": False,
        "message": f"Generated 1 image artifact ({size}).",
        "provider": _PROVIDER,
        "model": IMAGE_MODEL,
        "error": None,
        "remediation": None,
        "execution": {
            "provider": _PROVIDER,
            "model": IMAGE_MODEL,
            "host_model": image.host_model,
            "quality": quality,
            "requested_size": SIZES[aspect_ratio],
            "size": size,
            "fallback_used": False,
        },
        "images": [
            {
                "localPath": str(local) if local is not None else path,
                "path": path,
                "mediaType": "image/png",
                "sizeBytes": len(image.data),
            }
        ],
    }


def build_chatgpt_image_tools(
    *,
    workspace: BackendWorkspace,
    paths: PathLayout,
    provider_repo: Any,
    settings_store: ChatgptImageSettingsStore,
) -> list[StructuredTool]:
    """Return the tool bound to one agent's workspace."""

    async def chatgpt_image(
        prompt: Annotated[str, Field(description="Full description of the image to create.")],
        aspect_ratio: Annotated[
            AspectRatio,
            Field(
                description=(
                    "square (1024x1024), landscape (1536x1024) or portrait (1024x1536). "
                    "The service may return another size; the result reports the actual one."
                )
            ),
        ] = "square",
        quality: Annotated[
            Quality,
            Field(description="low is fastest and uses the least allowance; high is slowest."),
        ] = "medium",
    ) -> dict[str, object]:
        return await generate_to_workspace(
            workspace=workspace,
            paths=paths,
            provider_repo=provider_repo,
            settings_store=settings_store,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            quality=quality,
            acting_user_is_admin=_acting_user_is_admin(),
        )

    return [
        StructuredTool.from_function(
            coroutine=chatgpt_image,
            name=TOOL_NAME,
            description=_DESCRIPTION,
        )
    ]


__all__ = [
    "OUTPUT_DIR",
    "TOOL_NAME",
    "QuotaStatus",
    "build_chatgpt_image_tools",
    "generate_to_workspace",
    "read_quota_status",
]
