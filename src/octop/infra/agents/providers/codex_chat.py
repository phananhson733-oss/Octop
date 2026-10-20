"""Chat on a ChatGPT login: the Codex backend only speaks the Responses API.

octop-harness builds every OpenAI-protocol provider as a Chat Completions client. The
Codex backend (``chatgpt.com/backend-api/codex``) has no ``/chat/completions`` and
answers it with a Cloudflare challenge page, so every chat turn routed to a ChatGPT
model failed — including any turn with an image, which picks it as the only vision
model. Requests here take the shape the backend accepts from the image tool
(``codex_image.py``): streamed, nothing stored, the system prompt as ``instructions``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import Field

CODEX_BASE_URL_MARK = "chatgpt.com/backend-api/codex"

# Codex clients always send instructions; this one stands in when a call has no system prompt
_FALLBACK_INSTRUCTIONS = "You are a helpful assistant."
# Sampling and output caps the backend does not take (pydantic-ai's Codex provider drops them too)
_DROPPED_PARAMS = ("max_output_tokens", "temperature", "top_p")


def is_codex_base_url(base_url: str | None) -> bool:
    return bool(base_url and CODEX_BASE_URL_MARK in base_url)


def _item_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(block.get("text") or "") for block in content if isinstance(block, dict))
    return ""


class CodexChatOpenAI(ChatOpenAI):
    """``ChatOpenAI`` shaped for the Codex backend."""

    use_responses_api: bool | None = True
    store: bool | None = False
    # Nothing is stored, so reasoning is replayed from its encrypted copy
    include: list[str] | None = Field(default_factory=lambda: ["reasoning.encrypted_content"])

    def __init__(self, **kwargs: Any) -> None:
        # Streaming-only backend: invoke() must drain a stream too, and langchain only does
        # that when ``streaming`` was passed in, not when it is a field default
        kwargs.setdefault("streaming", True)
        super().__init__(**kwargs)

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        items = payload.get("input")
        if isinstance(items, list):
            system = [
                item for item in items if isinstance(item, dict) and item.get("role") == "system"
            ]
            payload["input"] = [item for item in items if item not in system]
            instructions = "\n\n".join(text for text in map(_item_text, system) if text)
            payload["instructions"] = instructions or _FALLBACK_INSTRUCTIONS
        for key in _DROPPED_PARAMS:
            payload.pop(key, None)
        return payload


def build_codex_chat_model(
    *,
    model_id: str,
    base_url: str,
    api_key: str,
    headers: dict[str, str] | None = None,
) -> CodexChatOpenAI:
    kwargs: dict[str, Any] = {"model": model_id, "base_url": base_url, "api_key": api_key}
    if headers:
        kwargs["default_headers"] = dict(headers)
    return CodexChatOpenAI(**kwargs)


def install_codex_chat_builder() -> None:
    """Have octop-harness build ChatGPT-login providers with :class:`CodexChatOpenAI`."""
    from octop_harness.llm import factory

    original: Callable[[Any, Any], Any] = factory._build_openai
    if getattr(original, "_octop_codex", False):
        return

    def build(provider: Any, model: Any) -> Any:
        if not is_codex_base_url(provider.base_url):
            return original(provider, model)
        instance = build_codex_chat_model(
            model_id=model.id,
            base_url=provider.base_url,
            api_key=provider.api_key,
            headers=provider.headers,
        )
        # The bookkeeping octop-harness puts on the models it builds
        object.__setattr__(instance, "_harness_supported_input", set(model.input))
        factory._stamp_native_tool_search_capability(
            instance, provider.protocol, model.native_tool_search
        )
        factory._inject_model_token_limits(instance, model)
        return instance

    build._octop_codex = True  # type: ignore[attr-defined]
    factory._build_openai = build


__all__ = [
    "CODEX_BASE_URL_MARK",
    "CodexChatOpenAI",
    "build_codex_chat_model",
    "install_codex_chat_builder",
    "is_codex_base_url",
]
