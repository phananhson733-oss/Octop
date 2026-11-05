"""Skip images the model provider refuses, then call the model again.

Some providers check every input image and fail the whole call when one is refused: Azure
gpt-6-luna answers ``content_policy_violation`` for ordinary short-drama frames. Behind
LiteLLM the stream then only carries ``response.failed``, which langchain-openai drops, so
the call ends as an empty AIMessage with no usage and no finish marker, and the turn stops
without a word. When that happens, check each image new to the request on its own, swap the
refused ones for a text note and call again. A failure that remains raises, so the user sees
an error instead of silence (the outer retry middleware retries it first).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

logger = logging.getLogger(__name__)

_FINISH_KEYS = ("status", "finish_reason", "stop_reason", "done_reason")
_PROBE_PROMPT = "Reply with OK."
# Probes must not reach the user's stream or the turn's callbacks.
_PROBE_CONFIG: Any = {"callbacks": [], "tags": ["nostream"]}
_PROBE_CONCURRENCY = 4
_BLOCKED_CACHE_MAX = 1024


class EmptyModelResponseError(RuntimeError):
    """The model stream ended without content, tool calls or a finish marker."""


def is_failed_stream(message: Any) -> bool:
    """True for the empty AIMessage a stream leaves behind when the provider failed it."""
    if not isinstance(message, AIMessage):
        return False
    if message.content or message.tool_calls or message.invalid_tool_calls:
        return False
    if message.usage_metadata is not None:
        return False
    meta = message.response_metadata or {}
    return not any(meta.get(key) for key in _FINISH_KEYS)


def _failed(response: ModelResponse[Any]) -> bool:
    messages = [m for m in response.result if isinstance(m, AIMessage)]
    return bool(messages) and is_failed_stream(messages[-1])


def _image_key(block: Any) -> str | None:
    """Hash of an inline image block's bytes; ``None`` for anything else."""
    if not isinstance(block, dict):
        return None
    data: Any = None
    if block.get("type") == "image":
        data = block.get("base64") or block.get("data")
        if not data and str(block.get("url") or "").startswith("data:"):
            data = block["url"]
    elif block.get("type") == "image_url":
        raw = block.get("image_url")
        url = raw.get("url") if isinstance(raw, dict) else raw
        if isinstance(url, str) and url.startswith("data:"):
            data = url
    if not isinstance(data, str) or not data:
        return None
    return hashlib.sha256(data.encode()).hexdigest()


def _new_images(messages: Sequence[BaseMessage]) -> dict[str, dict[str, Any]]:
    """Inline images after the last AI message: the only ones this call sends for the first time.

    Earlier images already went through the provider, and MediaOffload turns them into text
    on the next call anyway.
    """
    start = 0
    for index, msg in enumerate(messages):
        if isinstance(msg, AIMessage):
            start = index + 1
    found: dict[str, dict[str, Any]] = {}
    for msg in messages[start:]:
        if not isinstance(msg.content, list):
            continue
        for block in msg.content:
            key = _image_key(block)
            if key is not None and isinstance(block, dict):
                found.setdefault(key, block)
    return found


def _read_paths(messages: Sequence[BaseMessage]) -> dict[str, str]:
    """``tool_call_id`` → file path, so a note can say which file was skipped."""
    paths: dict[str, str] = {}
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        for call in msg.tool_calls:
            args = call.get("args") or {}
            path = args.get("file_path") or args.get("path")
            if call.get("id") and isinstance(path, str):
                paths[str(call["id"])] = path
    return paths


def _skip_note(path: str | None) -> dict[str, Any]:
    name = f" `{path}`" if path else ""
    return {
        "type": "text",
        "text": (
            f"[image{name} skipped: the model provider refused to process it (its image "
            "safety check). Do not read it again; work from the transcript and the other "
            "frames instead.]"
        ),
    }


def _model_id(model: Any) -> str:
    name = getattr(model, "model_name", None) or getattr(model, "model", None)
    return f"{type(model).__name__}:{name}"


def _probe_messages(block: dict[str, Any]) -> list[BaseMessage]:
    return [HumanMessage(content=[{"type": "text", "text": _PROBE_PROMPT}, block])]


class BlockedImageRetryMiddleware(AgentMiddleware[Any, Any]):
    """Replace provider-refused images with a note and retry the model call once."""

    def __init__(self) -> None:
        super().__init__()
        # Refused images by model and hash, so a re-read or an outer retry skips them
        # without probing, while another model still gets to see them.
        self._blocked: dict[tuple[str, str], None] = {}

    def _remember(self, model: Any, keys: Sequence[str]) -> None:
        for key in keys:
            self._blocked[(_model_id(model), key)] = None
        while len(self._blocked) > _BLOCKED_CACHE_MAX:
            self._blocked.pop(next(iter(self._blocked)))

    def _without_blocked(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        model = _model_id(request.model)
        blocked = {key for owner, key in self._blocked if owner == model}
        if not blocked:
            return request
        messages = list(request.messages)
        paths: dict[str, str] | None = None
        changed = False
        for index, msg in enumerate(messages):
            if not isinstance(msg.content, list) or not any(
                _image_key(block) in blocked for block in msg.content
            ):
                continue
            if paths is None:
                paths = _read_paths(messages)
            path = paths.get(msg.tool_call_id) if isinstance(msg, ToolMessage) else None
            content = [
                _skip_note(path) if _image_key(block) in blocked else block for block in msg.content
            ]
            messages[index] = msg.model_copy(update={"content": content})
            changed = True
        return request.override(messages=messages) if changed else request

    def _after_probe(self, model: Any, refused: list[str], total: int) -> None:
        if not refused:
            raise EmptyModelResponseError(
                "the model returned an empty response: the provider ended the stream "
                "without a result"
            )
        logger.warning(
            "BlockedImageRetry: provider refused %d of %d new image(s); retrying without them",
            len(refused),
            total,
        )
        self._remember(model, refused)

    @staticmethod
    def _check_retry(response: ModelResponse[Any]) -> ModelResponse[Any]:
        if _failed(response):
            raise EmptyModelResponseError(
                "the model returned an empty response again after refused images were skipped"
            )
        return response

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        request = self._without_blocked(request)
        response = handler(request)
        if not _failed(response):
            return response
        images = _new_images(request.messages)
        refused = []
        for key, block in images.items():
            # A probe that errors says nothing about the image; leave it to the outer retry.
            try:
                reply = request.model.invoke(_probe_messages(block), config=_PROBE_CONFIG)
            except Exception:
                logger.warning("BlockedImageRetry: image probe failed", exc_info=True)
                continue
            if is_failed_stream(reply):
                refused.append(key)
        self._after_probe(request.model, refused, len(images))
        return self._check_retry(handler(self._without_blocked(request)))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        request = self._without_blocked(request)
        response = await handler(request)
        if not _failed(response):
            return response
        images = _new_images(request.messages)
        gate = asyncio.Semaphore(_PROBE_CONCURRENCY)

        async def refused(block: dict[str, Any]) -> bool:
            async with gate:
                try:
                    reply = await request.model.ainvoke(
                        _probe_messages(block), config=_PROBE_CONFIG
                    )
                except Exception:
                    # An error says nothing about the image; leave it to the outer retry.
                    logger.warning("BlockedImageRetry: image probe failed", exc_info=True)
                    return False
            return is_failed_stream(reply)

        verdicts = await asyncio.gather(*(refused(block) for block in images.values()))
        self._after_probe(
            request.model,
            [key for key, bad in zip(images, verdicts, strict=True) if bad],
            len(images),
        )
        return self._check_retry(await handler(self._without_blocked(request)))


__all__ = ["BlockedImageRetryMiddleware", "EmptyModelResponseError", "is_failed_stream"]
