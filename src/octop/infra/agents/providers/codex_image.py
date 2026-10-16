"""Image generation on the ChatGPT subscription via the Codex Responses backend.

The ChatGPT login from :mod:`codex_oauth` can call the hosted
``image_generation`` tool (gpt-image-2) through a Codex chat model. The
endpoint is undocumented: it only accepts the chat models listed for the
account, and every image draws on the plan's Codex allowance first, then on
purchased credits. :func:`fetch_quota` reads the same usage endpoint the
Codex CLI uses so callers can stop before credits are spent.
"""

from __future__ import annotations

import base64
import binascii
import json
import struct
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from octop.infra.agents.providers.codex_oauth import CODEX_BASE_URL, build_codex_headers

IMAGE_MODEL = "gpt-image-2"
# Accepted for ChatGPT-account logins in 2026-10, while the gpt-5.4/5.5 that
# Octop's Codex provider shipped with were rejected. Tried before the
# provider row's own models.
DEFAULT_HOST_MODEL = "gpt-6-astra"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
MAX_HOST_MODEL_ATTEMPTS = 3
REQUEST_TIMEOUT = httpx.Timeout(300.0, connect=30.0)

AspectRatio = Literal["square", "landscape", "portrait"]
Quality = Literal["low", "medium", "high"]

SIZES: dict[str, str] = {
    "square": "1024x1024",
    "landscape": "1536x1024",
    "portrait": "1024x1536",
}

_INSTRUCTIONS = "Fulfill the image request by calling the image_generation tool."
_UNSUPPORTED_MODEL = "model is not supported"
_FAILURE_EVENTS = frozenset({"error", "response.failed", "response.incomplete"})
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class CodexImageError(Exception):
    """A classified failure; ``code`` drives the tool's remediation text."""

    def __init__(self, message: str, *, code: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class QuotaWindow:
    used_percent: float
    window_minutes: int | None
    reset_at: int | None


@dataclass(frozen=True)
class CodexQuota:
    plan_type: str | None
    limit_reached: bool
    windows: tuple[QuotaWindow, ...]
    has_credits: bool
    unlimited_credits: bool
    credits_balance: str | None

    @property
    def plan_exhausted(self) -> bool:
        """True when further use would be billed to credits (or refused)."""
        return self.limit_reached or any(w.used_percent >= 100 for w in self.windows)

    @property
    def resets_at(self) -> int | None:
        """Unix time when every exhausted window has reset, if known."""
        times = [w.reset_at for w in self.windows if w.used_percent >= 100 and w.reset_at]
        return max(times) if times else None


@dataclass(frozen=True)
class CodexImage:
    data: bytes
    width: int
    height: int
    host_model: str


def _headers(token: str, account_id: str, *, accept: str) -> dict[str, str]:
    headers = build_codex_headers(account_id)
    headers.update({"Authorization": f"Bearer {token}", "Accept": accept})
    return headers


def _window(raw: object, now: float) -> QuotaWindow | None:
    if not isinstance(raw, dict):
        return None
    used = raw.get("used_percent")
    if not isinstance(used, int | float):
        return None
    seconds = raw.get("limit_window_seconds")
    reset_at = raw.get("reset_at")
    if not isinstance(reset_at, int | float):
        after = raw.get("reset_after_seconds")
        reset_at = now + after if isinstance(after, int | float) else None
    return QuotaWindow(
        used_percent=float(used),
        window_minutes=int(seconds) // 60 if isinstance(seconds, int | float) else None,
        reset_at=int(reset_at) if reset_at is not None else None,
    )


def parse_usage(payload: object, *, now: float | None = None) -> CodexQuota:
    """Normalize the ``wham/usage`` body; raise when it carries no limit data."""
    if not isinstance(payload, dict):
        raise CodexImageError("ChatGPT usage response was not an object", code="quota_unknown")
    stamp = time.time() if now is None else now
    rate = payload.get("rate_limit")
    rate = rate if isinstance(rate, dict) else {}
    windows = tuple(
        window
        for window in (
            _window(rate.get(key), stamp) for key in ("primary_window", "secondary_window")
        )
        if window is not None
    )
    limit_reached = rate.get("limit_reached")
    if not windows and not isinstance(limit_reached, bool):
        raise CodexImageError("ChatGPT usage response has no rate-limit data", code="quota_unknown")
    credits = payload.get("credits")
    credits = credits if isinstance(credits, dict) else {}
    balance = credits.get("balance")
    plan_type = payload.get("plan_type")
    return CodexQuota(
        plan_type=str(plan_type) if plan_type else None,
        limit_reached=limit_reached is True,
        windows=windows,
        has_credits=credits.get("has_credits") is True,
        unlimited_credits=credits.get("unlimited") is True,
        credits_balance=str(balance) if balance is not None else None,
    )


async def fetch_quota(client: httpx.AsyncClient, *, token: str, account_id: str) -> CodexQuota:
    """Read the account's Codex usage windows and credit balance."""
    try:
        response = await client.get(
            USAGE_URL, headers=_headers(token, account_id, accept="application/json")
        )
    except httpx.HTTPError as exc:
        raise CodexImageError(f"Could not read ChatGPT usage: {exc}", code="quota_unknown") from exc
    if response.status_code == 401:
        raise CodexImageError(
            "ChatGPT rejected the stored login", code="auth_required", http_status=401
        )
    if response.status_code != 200:
        raise CodexImageError(
            f"ChatGPT usage returned HTTP {response.status_code}",
            code="quota_unknown",
            http_status=response.status_code,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise CodexImageError("ChatGPT usage response was not JSON", code="quota_unknown") from exc
    return parse_usage(payload)


def build_image_payload(
    *, prompt: str, host_model: str, aspect_ratio: AspectRatio, quality: Quality
) -> dict[str, Any]:
    """Responses request that forces one ``image_generation`` call."""
    return {
        "model": host_model,
        "store": False,
        "instructions": _INSTRUCTIONS,
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]}
        ],
        "tools": [
            {
                "type": "image_generation",
                "model": IMAGE_MODEL,
                "size": SIZES[aspect_ratio],
                "quality": quality,
                "output_format": "png",
                "background": "opaque",
                "partial_images": 1,
            }
        ],
        "tool_choice": {
            "type": "allowed_tools",
            "mode": "required",
            "tools": [{"type": "image_generation"}],
        },
        "stream": True,
    }


def _decode_sse(lines: list[str]) -> dict[str, Any] | None:
    raw = "\n".join(lines).strip()
    if not raw or raw == "[DONE]":
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


async def iter_sse_events(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    """Yield JSON ``data:`` payloads; tolerant of event types this code does not know."""
    data: list[str] = []
    async for line in lines:
        if line == "":
            event = _decode_sse(data)
            data = []
            if event is not None:
                yield event
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    event = _decode_sse(data)
    if event is not None:
        yield event


def _event_failure(event: dict[str, Any]) -> str:
    error = event.get("error")
    if not isinstance(error, dict):
        response = event.get("response")
        error = response.get("error") if isinstance(response, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return str(message or event.get("type"))[:300]


def _decode_png(b64: str) -> tuple[bytes, int, int]:
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CodexImageError(
            "ChatGPT returned undecodable image data", code="invalid_image"
        ) from exc
    if len(data) < 24 or not data.startswith(_PNG_MAGIC):
        raise CodexImageError("ChatGPT returned data that is not a PNG image", code="invalid_image")
    width, height = struct.unpack(">II", data[16:24])
    return data, int(width), int(height)


async def _generate_once(
    client: httpx.AsyncClient,
    *,
    token: str,
    account_id: str,
    host_model: str,
    prompt: str,
    aspect_ratio: AspectRatio,
    quality: Quality,
) -> CodexImage:
    payload = build_image_payload(
        prompt=prompt, host_model=host_model, aspect_ratio=aspect_ratio, quality=quality
    )
    final_b64: str | None = None
    failure: str | None = None
    async with client.stream(
        "POST",
        f"{CODEX_BASE_URL}/responses",
        json=payload,
        headers=_headers(token, account_id, accept="text/event-stream"),
    ) as response:
        if response.status_code != 200:
            body = (await response.aread()).decode(errors="replace")[:500]
            status = response.status_code
            if status == 400 and _UNSUPPORTED_MODEL in body:
                code = "unsupported_model"
            elif status == 401:
                code = "auth_required"
            elif status == 429:
                code = "rate_limited"
            else:
                code = "upstream_error"
            raise CodexImageError(
                f"ChatGPT returned HTTP {status}: {body}", code=code, http_status=status
            )
        async for event in iter_sse_events(response.aiter_lines()):
            if event.get("type") in _FAILURE_EVENTS:
                failure = _event_failure(event)
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "image_generation_call":
                result = item.get("result")
                if isinstance(result, str) and result:
                    final_b64 = result
    if final_b64 is None:
        if failure:
            raise CodexImageError(
                f"ChatGPT image generation failed: {failure}", code="generation_failed"
            )
        raise CodexImageError("ChatGPT returned no image", code="empty_result")
    data, width, height = _decode_png(final_b64)
    return CodexImage(data=data, width=width, height=height, host_model=host_model)


async def generate_image(
    client: httpx.AsyncClient,
    *,
    token: str,
    account_id: str,
    prompt: str,
    aspect_ratio: AspectRatio,
    quality: Quality,
    host_models: Sequence[str],
) -> CodexImage:
    """Generate one PNG, moving to the next host model only when one is refused."""
    candidates = list(dict.fromkeys(m for m in host_models if m))[:MAX_HOST_MODEL_ATTEMPTS]
    last: CodexImageError | None = None
    for host_model in candidates:
        try:
            return await _generate_once(
                client,
                token=token,
                account_id=account_id,
                host_model=host_model,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                quality=quality,
            )
        except CodexImageError as exc:
            if exc.code != "unsupported_model":
                raise
            last = exc
    raise last or CodexImageError("No ChatGPT model is available", code="unsupported_model")


__all__ = [
    "DEFAULT_HOST_MODEL",
    "IMAGE_MODEL",
    "REQUEST_TIMEOUT",
    "SIZES",
    "AspectRatio",
    "CodexImage",
    "CodexImageError",
    "CodexQuota",
    "Quality",
    "QuotaWindow",
    "build_image_payload",
    "fetch_quota",
    "generate_image",
    "iter_sse_events",
    "parse_usage",
]
