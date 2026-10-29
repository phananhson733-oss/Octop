"""ChatGPT-login chat models: Responses API requests shaped for the Codex backend."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from octop_harness.config import ModelConfig, ProviderConfig
from octop_harness.llm import factory

from octop.infra.agents.providers.codex_chat import (
    CodexChatOpenAI,
    build_codex_chat_model,
    install_codex_chat_builder,
)

CODEX_URL = "https://chatgpt.com/backend-api/codex"
_PNG_URI = "data:image/png;base64,iVBORw0KGgo="


@tool
def read_file(path: str) -> str:
    """Read a file."""
    return path


def _response(rid: str, *, status: str, output: list[Any]) -> dict[str, Any]:
    return {
        "id": rid,
        "object": "response",
        "created_at": 0,
        "status": status,
        "model": "gpt-6-astra",
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 2},
            "total_tokens": 15,
        }
        if status == "completed"
        else None,
    }


def _sse(events: list[dict[str, Any]]) -> bytes:
    lines = []
    for number, event in enumerate(events):
        event = {**event, "sequence_number": number}
        lines.append(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n")
    return "".join(lines).encode()


def _tool_call_turn() -> bytes:
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc-1"}
    call = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "read_file",
        "arguments": '{"path": "a.md"}',
        "status": "completed",
    }
    return _sse(
        [
            {
                "type": "response.created",
                "response": _response("resp_1", status="in_progress", output=[]),
            },
            {"type": "response.output_item.added", "output_index": 0, "item": reasoning},
            {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {**call, "arguments": ""},
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 1,
                "item_id": "fc_1",
                "delta": '{"path": "a.md"}',
            },
            {"type": "response.output_item.done", "output_index": 1, "item": call},
            {
                "type": "response.completed",
                "response": _response("resp_1", status="completed", output=[reasoning, call]),
            },
        ]
    )


def _text_turn(text: str) -> bytes:
    done = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    return _sse(
        [
            {
                "type": "response.created",
                "response": _response("resp_2", status="in_progress", output=[]),
            },
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**done, "status": "in_progress", "content": []},
            },
            {
                "type": "response.content_part.added",
                "output_index": 0,
                "item_id": "msg_1",
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
            {
                "type": "response.output_text.delta",
                "output_index": 0,
                "item_id": "msg_1",
                "content_index": 0,
                "delta": text,
            },
            {
                "type": "response.output_text.done",
                "output_index": 0,
                "item_id": "msg_1",
                "content_index": 0,
                "text": text,
            },
            {"type": "response.output_item.done", "output_index": 0, "item": done},
            {
                "type": "response.completed",
                "response": _response("resp_2", status="completed", output=[done]),
            },
        ]
    )


class _FakeBackend:
    """Answers ``/responses`` like the Codex backend; refuses anything else the way it did."""

    def __init__(self, replies: list[bytes]) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if not request.url.path.endswith("/codex/responses"):
            return httpx.Response(403, text="<html><title>Just a moment...</title></html>")
        body = json.loads(request.content)
        self.requests.append(body)
        if body.get("stream") is not True or body.get("store") is not False:
            return httpx.Response(400, json={"detail": "stream must be true and store false"})
        return httpx.Response(
            200, content=self.replies.pop(0), headers={"content-type": "text/event-stream"}
        )


def _on_fake_backend(backend: _FakeBackend) -> CodexChatOpenAI:
    return CodexChatOpenAI(
        model="gpt-6-astra",
        base_url=CODEX_URL,
        api_key="token",
        default_headers={"chatgpt-account-id": "acct"},
        http_client=httpx.Client(transport=httpx.MockTransport(backend)),
        http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(backend)),
    )


def test_payload_moves_the_system_prompt_into_instructions() -> None:
    model = build_codex_chat_model(model_id="gpt-6-astra", base_url=CODEX_URL, api_key="t")
    payload = model._get_request_payload(
        [
            SystemMessage("You write blog posts."),
            HumanMessage(
                content=[
                    {"type": "text", "text": "look"},
                    {"type": "image_url", "image_url": {"url": _PNG_URI}},
                ]
            ),
        ],
        temperature=0.3,
        max_tokens=100,
    )
    assert payload["instructions"] == "You write blog posts."
    assert all(item.get("role") != "system" for item in payload["input"])
    assert payload["store"] is False
    assert payload["include"] == ["reasoning.encrypted_content"]
    for key in ("temperature", "max_output_tokens", "max_tokens", "stream_options", "messages"):
        assert key not in payload
    image = payload["input"][0]["content"][1]
    assert image["type"] == "input_image"


def test_payload_without_a_system_prompt_still_sends_instructions() -> None:
    model = build_codex_chat_model(model_id="gpt-6-astra", base_url=CODEX_URL, api_key="t")
    payload = model._get_request_payload([HumanMessage("hi")])
    assert payload["instructions"]


@pytest.mark.asyncio
async def test_tool_round_trip_streams_and_replays_without_stored_ids() -> None:
    backend = _FakeBackend([_tool_call_turn(), _text_turn("Done: two fixes.")])
    model = _on_fake_backend(backend).bind_tools([read_file])
    history: list[Any] = [SystemMessage("You write blog posts."), HumanMessage("fix it")]

    first = None
    async for chunk in model.astream(history):
        first = chunk if first is None else first + chunk
    assert first is not None
    reply = AIMessage(
        content=first.content,
        tool_calls=first.tool_calls,
        id=first.id,
        additional_kwargs=first.additional_kwargs,
        response_metadata=first.response_metadata,
    )
    assert reply.tool_calls[0]["name"] == "read_file"
    assert reply.tool_calls[0]["args"] == {"path": "a.md"}

    history += [reply, ToolMessage("draft text", tool_call_id=reply.tool_calls[0]["id"])]
    second = await model.ainvoke(history)
    assert second.text == "Done: two fixes."
    assert second.usage_metadata is not None

    assert all(path.endswith("/codex/responses") for path in backend.paths)
    asked = backend.requests[0]
    assert asked["instructions"] == "You write blog posts."
    assert all(item.get("role") != "system" for item in asked["input"])
    assert [entry["name"] for entry in asked["tools"]] == ["read_file"]
    replay = backend.requests[1]
    assert replay["instructions"] == "You write blog posts."
    kinds = [item.get("type") or item.get("role") for item in replay["input"]]
    assert "function_call" in kinds and "function_call_output" in kinds
    reasoning = [item for item in replay["input"] if item.get("type") == "reasoning"]
    assert reasoning and all(item.get("encrypted_content") == "enc-1" for item in reasoning)
    assert not any(str(item.get("id") or "").startswith("msg_") for item in replay["input"])


def test_installed_builder_builds_codex_providers_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # Put the harness builder back after this test
    monkeypatch.setattr(factory, "_build_openai", factory._build_openai)
    install_codex_chat_builder()
    install_codex_chat_builder()  # idempotent
    codex = factory.build_chat_model(
        ProviderConfig(
            id="openai-codex", base_url=CODEX_URL, api_key="t", headers={"originator": "x"}
        ),
        ModelConfig(id="gpt-6-astra", input=["text", "image"], context_window=400_000),
    )
    assert isinstance(codex, CodexChatOpenAI)
    assert codex._harness_supported_input == {"text", "image"}  # type: ignore[attr-defined]
    assert codex.default_headers == {"originator": "x"}
    other = factory.build_chat_model(
        ProviderConfig(id="DeepSeek", base_url="https://api.deepseek.com/v1", api_key="t"),
        ModelConfig(id="deepseek-v4-flash"),
    )
    assert not isinstance(other, CodexChatOpenAI)


@pytest.mark.parametrize("base_url", [CODEX_URL, "https://api.deepseek.com/v1"])
def test_installed_builder_preserves_harness_retry_override(monkeypatch, base_url):
    monkeypatch.setattr(factory, "_build_openai", factory._build_openai)
    install_codex_chat_builder()
    model = factory._build_openai(
        ProviderConfig(id="test", base_url=base_url, api_key="test"),
        ModelConfig(id="test-model"),
        max_retries=0,
    )
    assert model.max_retries == 0
