"""Tests for CodexQuotaFallbackMiddleware."""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Any

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from octop.infra.agents.middleware import codex_quota_fallback as mod
from octop.infra.agents.middleware.blocked_image_retry import EmptyModelResponseError
from octop.infra.agents.middleware.codex_quota_fallback import CodexQuotaFallbackMiddleware
from octop.infra.agents.providers.codex_chat import build_codex_chat_model
from octop.infra.agents.providers.codex_image import CodexQuota, QuotaWindow
from octop.infra.agents.providers.codex_oauth import CODEX_BASE_URL

_FALLBACK_REF = "DeepSeek/deepseek-v4-flash"
_ACCOUNT = "acct-a"


@pytest.fixture(autouse=True)
def _fresh_state() -> Iterator[None]:
    mod._reset_exhausted_state()
    yield
    mod._reset_exhausted_state()


def _codex() -> Any:
    return build_codex_chat_model(
        model_id="gpt-6.1-sol", base_url=CODEX_BASE_URL, api_key="test", headers=None
    )


def _rate_limited() -> openai.RateLimitError:
    response = httpx.Response(429, request=httpx.Request("POST", f"{CODEX_BASE_URL}/responses"))
    return openai.RateLimitError("usage limit", response=response, body=None)


def _quota(used: float, reset_at: int | None = None) -> CodexQuota:
    return CodexQuota(
        plan_type="plus",
        limit_reached=used >= 100,
        windows=(QuotaWindow(used_percent=used, window_minutes=300, reset_at=reset_at),),
        has_credits=False,
        unlimited_credits=False,
        credits_balance=None,
    )


@dataclass
class _Request:
    messages: list[BaseMessage]
    model: Any

    def override(self, **kwargs: Any) -> _Request:
        return replace(self, **kwargs)


class _Handler:
    """Raises 429 for Codex models while *codex_limited*; records the models it was given."""

    def __init__(self, *, codex_limited: bool = True) -> None:
        self.codex_limited = codex_limited
        self.models: list[Any] = []

    def __call__(self, request: _Request) -> AIMessage:
        self.models.append(request.model)
        if self.codex_limited and isinstance(request.model, mod.CodexChatOpenAI):
            raise _rate_limited()
        return AIMessage(content="ok")

    async def acall(self, request: _Request) -> AIMessage:
        return self(request)


class _Built:
    def __init__(self) -> None:
        self.refs: list[str] = []
        self.model = object()

    def __call__(self, ref: str) -> Any:
        self.refs.append(ref)
        return self.model


def _middleware(quota: Any, built: _Built, account: str = _ACCOUNT) -> CodexQuotaFallbackMiddleware:
    def read() -> tuple[str, CodexQuota]:
        if isinstance(quota, Exception):
            raise quota
        return account, quota

    return CodexQuotaFallbackMiddleware(
        fallback_ref=_FALLBACK_REF,
        build_model=built,
        read_quota=read,
        read_account=lambda: account,
    )


def _request(model: Any) -> Any:
    return _Request(messages=[HumanMessage(content="cut it")], model=model)


async def test_used_up_plan_falls_back_and_is_remembered() -> None:
    built, handler = _Built(), _Handler()
    middleware = _middleware(_quota(100, reset_at=int(time.time()) + 3600), built)
    codex = _codex()

    result = await middleware.awrap_model_call(_request(codex), handler.acall)  # type: ignore[arg-type]

    assert result.content == "ok"
    assert handler.models == [codex, built.model]
    assert built.refs == [_FALLBACK_REF]

    # A second agent's middleware skips Codex until the reset.
    other_handler = _Handler()
    other = _middleware(AssertionError("allowance must not be re-read"), _Built())
    await other.awrap_model_call(_request(codex), other_handler.acall)  # type: ignore[arg-type]
    assert not isinstance(other_handler.models[0], mod.CodexChatOpenAI)


async def test_rate_limit_with_allowance_left_is_raised() -> None:
    built, handler = _Built(), _Handler()
    middleware = _middleware(_quota(40), built)

    with pytest.raises(openai.RateLimitError):
        await middleware.awrap_model_call(_request(_codex()), handler.acall)  # type: ignore[arg-type]

    assert built.refs == []
    assert mod._exhausted_until == {}


async def test_unreadable_allowance_falls_back_once() -> None:
    built, handler = _Built(), _Handler()
    middleware = _middleware(RuntimeError("no login"), built)

    await middleware.awrap_model_call(_request(_codex()), handler.acall)  # type: ignore[arg-type]

    assert built.refs == [_FALLBACK_REF]
    assert mod._exhausted_until == {}


async def test_codex_is_tried_again_after_the_reset() -> None:
    mod._exhausted_until[_ACCOUNT] = time.time() - 1
    built, handler = _Built(), _Handler(codex_limited=False)
    codex = _codex()

    await _middleware(_quota(0), built).awrap_model_call(_request(codex), handler.acall)  # type: ignore[arg-type]

    assert handler.models == [codex]
    assert built.refs == []


async def test_used_up_plan_without_reset_time_waits_a_default_window() -> None:
    built, handler = _Built(), _Handler()
    before = time.time()

    await _middleware(_quota(100), built).awrap_model_call(_request(_codex()), handler.acall)  # type: ignore[arg-type]

    assert mod._exhausted_until[_ACCOUNT] >= before + mod.UNKNOWN_RESET_S


async def test_other_models_pass_through() -> None:
    built, handler = _Built(), _Handler()
    mod._exhausted_until[_ACCOUNT] = time.time() + 3600
    other = object()

    await _middleware(_quota(100), built).awrap_model_call(_request(other), handler.acall)  # type: ignore[arg-type]

    assert handler.models == [other]
    assert built.refs == []


def test_sync_path_falls_back() -> None:
    built, handler = _Built(), _Handler()
    middleware = _middleware(_quota(100, reset_at=int(time.time()) + 60), built)

    result = middleware.wrap_model_call(_request(_codex()), handler)  # type: ignore[arg-type]

    assert result.content == "ok"
    assert built.refs == [_FALLBACK_REF]


async def test_another_login_tries_codex_again() -> None:
    mod._exhausted_until[_ACCOUNT] = time.time() + 3600
    built, handler = _Built(), _Handler(codex_limited=False)
    codex = _codex()

    await _middleware(_quota(0), built, account="acct-b").awrap_model_call(
        _request(codex),
        handler.acall,  # type: ignore[arg-type]
    )

    assert handler.models == [codex]
    assert built.refs == []


async def test_fallback_gets_history_without_codex_reasoning() -> None:
    built, handler = _Built(), _Handler()
    reply = AIMessage(
        content=[
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc-1"},
            {"type": "function_call", "name": "read_file", "call_id": "call_1", "id": "fc_1"},
            {"type": "text", "text": "looking", "id": "msg_1"},
        ],
        tool_calls=[{"name": "read_file", "args": {}, "id": "call_1"}],
        additional_kwargs={"reasoning": {"id": "rs_1"}},
    )
    request = _Request(messages=[HumanMessage(content="cut it"), reply], model=_codex())
    sent: list[_Request] = []

    async def record(req: _Request) -> AIMessage:
        sent.append(req)
        return await handler.acall(req)

    await _middleware(_quota(100), built).awrap_model_call(request, record)  # type: ignore[arg-type]

    replayed = sent[-1].messages[1]
    assert isinstance(replayed, AIMessage)
    assert replayed.content == [
        {"type": "function_call", "name": "read_file", "call_id": "call_1"},
        {"type": "text", "text": "looking"},
    ]
    assert replayed.tool_calls[0]["id"] == "call_1"
    assert replayed.additional_kwargs == {}
    # The Codex call itself still sends the original history.
    assert sent[0].messages[1] is reply


async def test_failed_stream_with_used_up_plan_falls_back() -> None:
    built = _Built()
    calls: list[Any] = []

    async def handler(req: _Request) -> AIMessage:
        calls.append(req.model)
        if isinstance(req.model, mod.CodexChatOpenAI):
            raise EmptyModelResponseError("empty")
        return AIMessage(content="ok")

    await _middleware(_quota(100), built).awrap_model_call(_request(_codex()), handler)  # type: ignore[arg-type]
    assert built.refs == [_FALLBACK_REF]

    mod._reset_exhausted_state()
    with pytest.raises(EmptyModelResponseError):
        await _middleware(_quota(10), _Built()).awrap_model_call(_request(_codex()), handler)  # type: ignore[arg-type]
