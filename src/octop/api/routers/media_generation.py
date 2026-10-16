"""Admin API for instance-wide image and video generation settings."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from octop.api.deps import get_server, require_permission
from octop.infra.agents.providers.codex_image_tool import QuotaStatus, read_quota_status
from octop.infra.agents.settings.chatgpt_image import ChatgptImageSettings
from octop.infra.agents.settings.media_generation import (
    MEDIA_PROVIDER_PRESETS,
    MediaGenerationSettings,
    MediaProviderName,
    MediaProviderSettings,
    MediaProviderUpdate,
)

router = APIRouter()


class MediaProviderPresetResponse(BaseModel):
    provider: MediaProviderName
    display_name: str
    base_url: str
    image_models: list[str]
    video_models: list[str]


class MediaProviderResponse(BaseModel):
    id: str
    provider: MediaProviderName
    display_name: str
    enabled: bool
    base_url: str
    image_enabled: bool
    video_enabled: bool
    image_model: str
    video_model: str
    api_key_set: bool
    configured: bool


class MediaGenerationSettingsResponse(BaseModel):
    enabled: bool
    providers: list[MediaProviderResponse]
    default_image_provider: str | None
    default_video_provider: str | None
    configured: bool


class MediaProviderBody(BaseModel):
    id: str = Field(description="Stable provider-instance routing ID.")
    provider: MediaProviderName
    display_name: str
    enabled: bool = True
    base_url: str
    image_enabled: bool = True
    video_enabled: bool = True
    image_model: str
    video_model: str
    api_key: str | None = Field(
        default=None,
        description="Write-only API key; omit to keep the stored value.",
    )
    clear_api_key: bool = Field(
        default=False,
        description="Delete the stored API key for this provider instance.",
    )

    def to_domain(self) -> MediaProviderUpdate:
        return MediaProviderUpdate(**self.model_dump())


class MediaGenerationSettingsBody(BaseModel):
    enabled: bool = False
    providers: list[MediaProviderBody] = Field(default_factory=list, max_length=8)
    default_image_provider: str | None = None
    default_video_provider: str | None = None


class MediaGenerationTestBody(BaseModel):
    kind: Literal["credentials", "image", "video"] = "credentials"
    provider: MediaProviderBody


class MediaGenerationTestResponse(BaseModel):
    ok: bool
    error: str | None = None


class ChatgptImageSettingsBody(BaseModel):
    enabled: bool = Field(default=False, description="Mount the chatgpt_image tool.")
    admin_only: bool = Field(
        default=True,
        description="Only agents owned by admins get the tool, and only admins may call it.",
    )
    allow_credits: bool = Field(
        default=False,
        description=(
            "Keep generating after the plan's Codex allowance is used up, which bills "
            "purchased ChatGPT credits. When false, each call checks the allowance first "
            "and stops if it is exhausted or cannot be read."
        ),
    )


class ChatgptQuotaWindowResponse(BaseModel):
    used_percent: float
    window_minutes: int | None
    reset_at: int | None = Field(description="Unix seconds.")


class ChatgptQuotaResponse(BaseModel):
    plan_type: str | None
    plan_exhausted: bool
    resets_at: int | None = Field(description="Unix seconds when every used-up window resets.")
    windows: list[ChatgptQuotaWindowResponse]
    has_credits: bool
    unlimited_credits: bool
    credits_balance: str | None


class ChatgptImageStatusResponse(BaseModel):
    enabled: bool
    admin_only: bool
    allow_credits: bool
    logged_in: bool = Field(description="A ChatGPT login is stored on this instance.")
    quota: ChatgptQuotaResponse | None = Field(
        description="Live allowance from ChatGPT; null when not logged in or unreadable."
    )
    quota_error: str | None


async def _chatgpt_image_response(
    server: Any, settings: ChatgptImageSettings
) -> ChatgptImageStatusResponse:
    status: QuotaStatus = await read_quota_status(
        server.services.paths, server.services.provider_repo
    )
    quota = status.quota
    return ChatgptImageStatusResponse(
        enabled=settings.enabled,
        admin_only=settings.admin_only,
        allow_credits=settings.allow_credits,
        logged_in=status.logged_in,
        quota=(
            ChatgptQuotaResponse(
                plan_type=quota.plan_type,
                plan_exhausted=quota.plan_exhausted,
                resets_at=quota.resets_at,
                windows=[
                    ChatgptQuotaWindowResponse(
                        used_percent=w.used_percent,
                        window_minutes=w.window_minutes,
                        reset_at=w.reset_at,
                    )
                    for w in quota.windows
                ],
                has_credits=quota.has_credits,
                unlimited_credits=quota.unlimited_credits,
                credits_balance=quota.credits_balance,
            )
            if quota is not None
            else None
        ),
        quota_error=status.error,
    )


def _provider_response(view: MediaProviderSettings) -> MediaProviderResponse:
    return MediaProviderResponse(
        id=view.id,
        provider=view.provider,
        display_name=view.display_name,
        enabled=view.enabled,
        base_url=view.base_url,
        image_enabled=view.image_enabled,
        video_enabled=view.video_enabled,
        image_model=view.image_model,
        video_model=view.video_model,
        api_key_set=view.api_key_set,
        configured=view.configured,
    )


def _response(view: MediaGenerationSettings) -> MediaGenerationSettingsResponse:
    return MediaGenerationSettingsResponse(
        enabled=view.enabled,
        providers=[_provider_response(item) for item in view.providers],
        default_image_provider=view.default_image_provider,
        default_video_provider=view.default_video_provider,
        configured=view.configured,
    )


@router.get(
    "/presets",
    summary="List built-in media provider presets",
    description="Return supported provider adapters and their default image and video models.",
    response_model=list[MediaProviderPresetResponse],
)
async def list_media_provider_presets(
    _: Any = Depends(require_permission("providers")),
) -> list[MediaProviderPresetResponse]:
    return [
        MediaProviderPresetResponse(
            provider=item.provider,
            display_name=item.display_name,
            base_url=item.base_url,
            image_models=list(item.image_models),
            video_models=list(item.video_models),
        )
        for item in MEDIA_PROVIDER_PRESETS.values()
    ]


@router.get(
    "",
    summary="Get media generation settings",
    description=(
        "Return instance-wide provider instances and default image/video routes. "
        "Stored API keys are never returned."
    ),
    response_model=MediaGenerationSettingsResponse,
)
async def get_media_generation_settings(
    _: Any = Depends(require_permission("providers")),
    server: Any = Depends(get_server),
) -> MediaGenerationSettingsResponse:
    return _response(server.app_runtime.agent_registry.media_generation.load())


@router.put(
    "",
    summary="Update media generation settings",
    description=(
        "Persist provider instances and deterministic default routes, then reload "
        "running agents. API keys are write-only; omit them to retain stored values."
    ),
    response_model=MediaGenerationSettingsResponse,
)
async def put_media_generation_settings(
    body: MediaGenerationSettingsBody,
    _: Any = Depends(require_permission("providers")),
    server: Any = Depends(get_server),
) -> MediaGenerationSettingsResponse:
    view = await server.app_runtime.agent_registry.save_media_generation(
        enabled=body.enabled,
        providers=[item.to_domain() for item in body.providers],
        default_image_provider=body.default_image_provider,
        default_video_provider=body.default_video_provider,
    )
    return _response(view)


@router.post(
    "/test",
    summary="Test a media provider configuration",
    description=(
        "Test credentials without generation, or submit a real image/video model "
        "request. Model tests may incur provider charges."
    ),
    response_model=MediaGenerationTestResponse,
)
async def test_media_generation_provider(
    body: MediaGenerationTestBody,
    _: Any = Depends(require_permission("providers")),
    server: Any = Depends(get_server),
) -> MediaGenerationTestResponse:
    store = server.app_runtime.agent_registry.media_generation
    provider = body.provider.to_domain()
    if body.kind == "credentials":
        result = await store.test_connection(provider)
    else:
        result = await store.test_model(provider, kind=body.kind)
    return MediaGenerationTestResponse(
        ok=bool(result.get("ok")),
        error=str(result["error"]) if result.get("error") else None,
    )


@router.get(
    "/chatgpt-image",
    summary="Get ChatGPT-subscription image settings",
    description=(
        "Return the chatgpt_image switches, whether a ChatGPT login is stored, and the "
        "live Codex allowance read from ChatGPT (no image is generated)."
    ),
    response_model=ChatgptImageStatusResponse,
)
async def get_chatgpt_image_settings(
    _: Any = Depends(require_permission("providers")),
    server: Any = Depends(get_server),
) -> ChatgptImageStatusResponse:
    settings = server.app_runtime.agent_registry.chatgpt_image.load()
    return await _chatgpt_image_response(server, settings)


@router.put(
    "/chatgpt-image",
    summary="Update ChatGPT-subscription image settings",
    description=(
        "Persist the chatgpt_image switches and reload running agents so the tool is "
        "mounted or removed."
    ),
    response_model=ChatgptImageStatusResponse,
)
async def put_chatgpt_image_settings(
    body: ChatgptImageSettingsBody,
    _: Any = Depends(require_permission("providers")),
    server: Any = Depends(get_server),
) -> ChatgptImageStatusResponse:
    settings = await server.app_runtime.agent_registry.save_chatgpt_image(
        ChatgptImageSettings(**body.model_dump())
    )
    return await _chatgpt_image_response(server, settings)


__all__ = ["router"]
