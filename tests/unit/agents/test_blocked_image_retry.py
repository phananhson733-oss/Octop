"""Tests for BlockedImageRetryMiddleware."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pytest
from langchain.agents.middleware import ModelResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from octop.infra.agents.middleware.blocked_image_retry import (
    BlockedImageRetryMiddleware,
    EmptyModelResponseError,
    is_failed_stream,
)

_FAILED = AIMessage(content=[], response_metadata={"model_provider": "openai", "id": "resp_1"})
_DONE = AIMessage(
    content="ok",
    response_metadata={"status": "completed"},
    usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
)


def _image(data: str) -> dict[str, Any]:
    return {"type": "image", "base64": data, "mime_type": "image/jpeg"}


class _Model:
    """Refuses any probe whose image bytes are in *refused*."""

    def __init__(self, refused: set[str], name: str = "luna", error: bool = False) -> None:
        self.refused = refused
        self.model_name = name
        self.error = error
        self.probes: list[str] = []

    def _reply(self, messages: list[BaseMessage]) -> AIMessage:
        block = messages[0].content[1]
        assert isinstance(block, dict)
        self.probes.append(block["base64"])
        if self.error:
            raise TimeoutError
        return _FAILED if block["base64"] in self.refused else _DONE

    def invoke(self, messages: list[BaseMessage], config: Any = None) -> AIMessage:
        assert config["callbacks"] == []
        return self._reply(messages)

    async def ainvoke(self, messages: list[BaseMessage], config: Any = None) -> AIMessage:
        assert config["callbacks"] == []
        return self._reply(messages)


@dataclass
class _Request:
    messages: list[BaseMessage]
    model: _Model

    def override(self, **kwargs: Any) -> _Request:
        return replace(self, **kwargs)


def _frames_request(model: _Model) -> _Request:
    read = AIMessage(
        content="",
        tool_calls=[
            {"name": "read_file", "args": {"file_path": f"/w/f{n}.jpg"}, "id": f"c{n}"}
            for n in (1, 2, 3)
        ],
    )
    tools = [ToolMessage(content=[_image(f"frame{n}")], tool_call_id=f"c{n}") for n in (1, 2, 3)]
    return _Request(messages=[HumanMessage(content="cut it"), read, *tools], model=model)


def _sent_images(request: _Request) -> list[str]:
    return [
        block["base64"]
        for msg in request.messages
        if isinstance(msg.content, list)
        for block in msg.content
        if isinstance(block, dict) and block.get("type") == "image"
    ]


def _models_failing_on(bad: set[str]) -> tuple[list[_Request], Any]:
    seen: list[_Request] = []

    async def handler(request: Any) -> ModelResponse[Any]:
        seen.append(request)
        sent = set(_sent_images(request))
        return ModelResponse(result=[_FAILED if sent & bad else _DONE])

    return seen, handler


def test_failed_stream_signature() -> None:
    assert is_failed_stream(_FAILED)
    assert not is_failed_stream(_DONE)
    # Chat Completions without usage still ends with a finish reason.
    assert not is_failed_stream(AIMessage(content="", response_metadata={"finish_reason": "stop"}))
    assert not is_failed_stream(
        AIMessage(content="", tool_calls=[{"name": "ls", "args": {}, "id": "x"}])
    )


async def test_refused_frame_is_replaced_and_call_retried() -> None:
    model = _Model(refused={"frame2"})
    seen, handler = _models_failing_on({"frame2"})
    mw = BlockedImageRetryMiddleware()

    response = await mw.awrap_model_call(_frames_request(model), handler)

    assert response.result == [_DONE]
    assert len(seen) == 2
    assert _sent_images(seen[1]) == ["frame1", "frame3"]
    note = seen[1].messages[3].content[0]
    assert note["type"] == "text"
    assert "/w/f2.jpg" in note["text"]
    assert sorted(model.probes) == ["frame1", "frame2", "frame3"]


async def test_known_refused_frame_is_skipped_without_probing() -> None:
    model = _Model(refused={"frame2"})
    seen, handler = _models_failing_on({"frame2"})
    mw = BlockedImageRetryMiddleware()
    await mw.awrap_model_call(_frames_request(model), handler)
    model.probes.clear()
    seen.clear()

    response = await mw.awrap_model_call(_frames_request(model), handler)

    assert response.result == [_DONE]
    assert len(seen) == 1
    assert _sent_images(seen[0]) == ["frame1", "frame3"]
    assert model.probes == []


async def test_failure_without_refused_images_raises() -> None:
    model = _Model(refused=set())

    async def handler(_request: Any) -> ModelResponse[Any]:
        return ModelResponse(result=[_FAILED])

    with pytest.raises(EmptyModelResponseError):
        await BlockedImageRetryMiddleware().awrap_model_call(_frames_request(model), handler)


async def test_failure_after_skipping_raises() -> None:
    model = _Model(refused={"frame1"})

    async def handler(_request: Any) -> ModelResponse[Any]:
        return ModelResponse(result=[_FAILED])

    with pytest.raises(EmptyModelResponseError, match="again"):
        await BlockedImageRetryMiddleware().awrap_model_call(_frames_request(model), handler)


async def test_success_passes_through_untouched() -> None:
    model = _Model(refused=set())
    seen, handler = _models_failing_on(set())
    request = _frames_request(model)

    response = await BlockedImageRetryMiddleware().awrap_model_call(request, handler)

    assert response.result == [_DONE]
    assert seen == [request]
    assert model.probes == []


def test_sync_path_replaces_refused_frame() -> None:
    model = _Model(refused={"frame3"})
    seen: list[_Request] = []

    def handler(request: Any) -> ModelResponse[Any]:
        seen.append(request)
        return ModelResponse(result=[_FAILED if "frame3" in _sent_images(request) else _DONE])

    response = BlockedImageRetryMiddleware().wrap_model_call(_frames_request(model), handler)

    assert response.result == [_DONE]
    assert _sent_images(seen[-1]) == ["frame1", "frame2"]


async def test_probe_errors_do_not_mark_images_refused() -> None:
    seen, handler = _models_failing_on({"frame2"})
    mw = BlockedImageRetryMiddleware()

    with pytest.raises(EmptyModelResponseError):
        await mw.awrap_model_call(_frames_request(_Model(refused=set(), error=True)), handler)

    response = await mw.awrap_model_call(_frames_request(_Model(refused={"frame2"})), handler)
    assert response.result == [_DONE]
    assert _sent_images(seen[-1]) == ["frame1", "frame3"]


async def test_refusals_are_kept_per_model() -> None:
    seen, handler = _models_failing_on({"frame2"})
    mw = BlockedImageRetryMiddleware()
    await mw.awrap_model_call(_frames_request(_Model(refused={"frame2"})), handler)

    other = _Model(refused=set(), name="sol")
    _ok_seen, ok_handler = _models_failing_on(set())
    await mw.awrap_model_call(_frames_request(other), ok_handler)

    assert _sent_images(_ok_seen[0]) == ["frame1", "frame2", "frame3"]
