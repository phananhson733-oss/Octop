"""Unit tests for ChatGPT-subscription image generation (Codex Responses backend)."""

from __future__ import annotations

import base64
import json
import struct

import httpx
import pytest

from octop.infra.agents.providers.codex_image import (
    USAGE_URL,
    CodexImageError,
    build_image_payload,
    fetch_quota,
    generate_image,
    iter_sse_events,
    parse_usage,
)
from octop.infra.agents.providers.codex_oauth import CODEX_BASE_URL

RESPONSES_URL = f"{CODEX_BASE_URL}/responses"


def fake_png(width: int = 1536, height: int = 1024) -> bytes:
    ihdr = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + ihdr + b"\x00\x00\x00\x00"


def sse(*events: dict[str, object]) -> bytes:
    chunks = [f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events]
    return ("".join(chunks) + "data: [DONE]\n\n").encode()


def image_events(png: bytes) -> bytes:
    return sse(
        {"type": "response.created"},
        {"type": "response.image_generation_call.partial_image", "partial_image_b64": "AAAA"},
        {
            "type": "response.output_item.done",
            "item": {"type": "image_generation_call", "result": base64.b64encode(png).decode()},
        },
        {"type": "response.completed", "response": {"usage": {"total_tokens": 10}}},
    )


def test_parse_usage_marks_exhausted_week_and_credit_balance() -> None:
    quota = parse_usage(
        {
            "plan_type": "pro",
            "rate_limit": {
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 100,
                    "limit_window_seconds": 604800,
                    "reset_at": 1792649637,
                },
                "secondary_window": None,
            },
            "credits": {"has_credits": True, "unlimited": False, "balance": "1848.22"},
        }
    )

    assert quota.plan_type == "pro"
    assert quota.plan_exhausted is True
    assert quota.resets_at == 1792649637
    assert quota.windows[0].window_minutes == 10080
    assert quota.has_credits is True
    assert quota.credits_balance == "1848.22"


def test_parse_usage_under_limit_uses_reset_after_seconds() -> None:
    quota = parse_usage(
        {
            "rate_limit": {
                "primary_window": {"used_percent": 42.5, "reset_after_seconds": 60},
            }
        },
        now=1000.0,
    )

    assert quota.plan_exhausted is False
    assert quota.windows[0].reset_at == 1060
    assert quota.resets_at is None


def test_parse_usage_without_limit_data_is_unknown() -> None:
    with pytest.raises(CodexImageError) as exc:
        parse_usage({"plan_type": "pro"})
    assert exc.value.code == "quota_unknown"


def test_payload_forces_one_image_generation_call() -> None:
    payload = build_image_payload(
        prompt="a lighthouse", host_model="gpt-6-astra", aspect_ratio="portrait", quality="low"
    )

    assert payload["model"] == "gpt-6-astra"
    assert payload["stream"] is True
    assert payload["store"] is False
    tool = payload["tools"][0]
    assert tool["type"] == "image_generation"
    assert tool["model"] == "gpt-image-2"
    assert tool["size"] == "1024x1536"
    assert tool["quality"] == "low"
    assert payload["tool_choice"]["mode"] == "required"


async def test_iter_sse_events_joins_data_lines_and_skips_done() -> None:
    async def lines():
        for line in ["event: a", 'data: {"type":', 'data: "a"}', "", ": ping", "data: [DONE]", ""]:
            yield line

    events = [event async for event in iter_sse_events(lines())]

    assert events == [{"type": "a"}]


async def test_generate_image_moves_past_a_refused_host_model() -> None:
    png = fake_png()
    seen: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((body["model"], dict(request.headers)))
        if body["model"] == "gpt-5.4":
            return httpx.Response(
                400,
                json={
                    "detail": "The 'gpt-5.4' model is not supported when using Codex "
                    "with a ChatGPT account."
                },
            )
        return httpx.Response(
            200, content=image_events(png), headers={"content-type": "text/event-stream"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        image = await generate_image(
            client,
            token="tok",
            account_id="acct-1",
            prompt="a lighthouse",
            aspect_ratio="square",
            quality="low",
            host_models=["gpt-5.4", "gpt-6-astra", "gpt-6-astra"],
        )

    assert image.data == png
    assert (image.width, image.height) == (1536, 1024)
    assert image.host_model == "gpt-6-astra"
    assert [model for model, _ in seen] == ["gpt-5.4", "gpt-6-astra"]
    headers = seen[-1][1]
    assert headers["authorization"] == "Bearer tok"
    assert headers["chatgpt-account-id"] == "acct-1"
    assert headers["originator"] == "openclaw"


async def test_generate_image_does_not_retry_other_errors() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"detail": "unauthorized"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CodexImageError) as exc:
            await generate_image(
                client,
                token="tok",
                account_id="",
                prompt="x",
                aspect_ratio="square",
                quality="low",
                host_models=["gpt-6-astra", "gpt-6-luna"],
            )

    assert exc.value.code == "auth_required"
    assert calls == 1


async def test_generate_image_reports_a_failed_response_without_image() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse(
                {"type": "response.created"},
                {"type": "response.failed", "response": {"error": {"message": "moderation"}}},
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CodexImageError) as exc:
            await generate_image(
                client,
                token="tok",
                account_id="",
                prompt="x",
                aspect_ratio="square",
                quality="low",
                host_models=["gpt-6-astra"],
            )

    assert exc.value.code == "generation_failed"
    assert "moderation" in str(exc.value)


async def test_generate_image_ignores_partial_images_without_a_final_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse(
                {
                    "type": "response.image_generation_call.partial_image",
                    "partial_image_b64": base64.b64encode(fake_png()).decode(),
                },
                {"type": "response.completed"},
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CodexImageError) as exc:
            await generate_image(
                client,
                token="tok",
                account_id="",
                prompt="x",
                aspect_ratio="square",
                quality="low",
                host_models=["gpt-6-astra"],
            )

    assert exc.value.code == "empty_result"


async def test_fetch_quota_treats_errors_as_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == USAGE_URL
        return httpx.Response(503, text="busy")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CodexImageError) as exc:
            await fetch_quota(client, token="tok", account_id="acct")

    assert exc.value.code == "quota_unknown"
    assert exc.value.http_status == 503
