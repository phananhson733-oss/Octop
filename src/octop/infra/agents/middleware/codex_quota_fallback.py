"""Answer with another model while the ChatGPT subscription's Codex allowance is used up.

Agents on the ChatGPT login (``openai-codex/...``) stop working when the plan's Codex
allowance runs out: the backend answers 429 until the window resets (or fails the stream, which
reaches here as an empty reply). On either from a Codex model, read the allowance from the usage
endpoint. When the plan is used up, remember it until
the reset time and send this and every later call to the configured fallback model; one login
serves every agent on the instance, so the state is shared, per ChatGPT account. A 429 while
allowance remains is raised again for the outer retry. If the allowance cannot be read, only this
call falls back.

The fallback gets the history without Codex's encrypted reasoning and Responses item ids: another
account or provider cannot verify them and refuses the call. Text-only fallbacks need nothing
else: octop-harness strips image blocks for models whose provider row does not list image input.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import openai
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, BaseMessage

from octop.infra.agents.middleware.blocked_image_retry import EmptyModelResponseError
from octop.infra.agents.providers.codex_chat import CodexChatOpenAI
from octop.infra.agents.providers.codex_image import USAGE_URL, CodexQuota, parse_usage
from octop.infra.agents.providers.codex_oauth import (
    build_codex_headers,
    get_valid_access_token,
    load_codex_token,
)
from octop.infra.utils.paths import PathLayout

logger = logging.getLogger(__name__)

# Used when the usage endpoint says the plan is used up but gives no reset time.
UNKNOWN_RESET_S = 15 * 60
_USAGE_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# (account id, allowance) of the stored ChatGPT login
QuotaReader = Callable[[], tuple[str, CodexQuota]]
AccountReader = Callable[[], str | None]

# ChatGPT account id → Unix time until which its Codex calls go straight to the fallback.
_exhausted_until: dict[str, float] = {}


def read_codex_account(paths: PathLayout) -> str | None:
    """Account id of the stored ChatGPT login (blocking)."""
    cred = load_codex_token(paths)
    return cred["account_id"] if cred is not None else None


def read_codex_quota(paths: PathLayout) -> tuple[str, CodexQuota]:
    """Read the stored ChatGPT login's account id and Codex allowance (blocking)."""
    token = get_valid_access_token(paths)
    cred = load_codex_token(paths)
    if not token or cred is None:
        raise RuntimeError("no usable ChatGPT login is stored")
    headers = build_codex_headers(cred["account_id"])
    headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    response = httpx.get(USAGE_URL, headers=headers, timeout=_USAGE_TIMEOUT)
    response.raise_for_status()
    return cred["account_id"], parse_usage(response.json())


def _may_be_used_up(exc: BaseException) -> bool:
    if isinstance(exc, EmptyModelResponseError):
        return True
    return isinstance(exc, openai.APIStatusError) and exc.status_code == 429


def _reset_exhausted_state() -> None:
    """Forget remembered exhausted plans (tests)."""
    _exhausted_until.clear()


def _portable[M: BaseMessage](message: M) -> M:
    """An AI message without Codex's encrypted reasoning and Responses item ids."""
    if not isinstance(message, AIMessage) or not isinstance(message.content, list):
        return message
    content: list[str | dict[Any, Any]] = []
    for block in message.content:
        if isinstance(block, dict):
            if block.get("type") == "reasoning":
                continue
            block = {key: value for key, value in block.items() if key != "id"}
        content.append(block)
    extra = {k: v for k, v in message.additional_kwargs.items() if k != "reasoning"}
    return message.model_copy(update={"content": content, "additional_kwargs": extra})


class CodexQuotaFallbackMiddleware(AgentMiddleware[Any, Any]):
    """Swap a Codex model for ``fallback_ref`` while the subscription allowance is used up."""

    def __init__(
        self,
        *,
        fallback_ref: str,
        build_model: Callable[[str], Any],
        read_quota: QuotaReader,
        read_account: AccountReader,
    ) -> None:
        super().__init__()
        self._fallback_ref = fallback_ref
        self._build_model = build_model
        self._read_quota = read_quota
        self._read_account = read_account

    def _fallback(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        return request.override(
            model=self._build_model(self._fallback_ref),
            messages=[_portable(m) for m in request.messages],
        )

    def _known_used_up(self) -> bool:
        """True while the logged-in account's plan is remembered as used up (blocking)."""
        if not _exhausted_until:
            return False
        account = self._read_account()
        return account is not None and time.time() < _exhausted_until.get(account, 0.0)

    def _plan_used_up(self, read: tuple[str, CodexQuota] | None) -> bool:
        """Whether to fall back after a failed call; remembers a used-up plan for every agent."""
        if read is None:
            logger.warning(
                "CodexQuotaFallback: call failed and the allowance could not be read; using %s once",
                self._fallback_ref,
            )
            return True
        account, quota = read
        if not quota.plan_exhausted:
            return False
        until = float(quota.resets_at or time.time() + UNKNOWN_RESET_S)
        _exhausted_until[account] = until
        logger.warning(
            "CodexQuotaFallback: ChatGPT allowance used up until %s; using %s",
            time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(until)),
            self._fallback_ref,
        )
        return True

    def _read_or_none(self) -> tuple[str, CodexQuota] | None:
        try:
            return self._read_quota()
        except Exception:
            logger.warning("CodexQuotaFallback: reading the allowance failed", exc_info=True)
            return None

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        if not isinstance(request.model, CodexChatOpenAI):
            return handler(request)
        if self._known_used_up():
            return handler(self._fallback(request))
        try:
            return handler(request)
        except Exception as exc:
            if not _may_be_used_up(exc) or not self._plan_used_up(self._read_or_none()):
                raise
        return handler(self._fallback(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        if not isinstance(request.model, CodexChatOpenAI):
            return await handler(request)
        if await asyncio.to_thread(self._known_used_up):
            return await handler(self._fallback(request))
        try:
            return await handler(request)
        except Exception as exc:
            if not _may_be_used_up(exc):
                raise
            read = await asyncio.to_thread(self._read_or_none)
            if not self._plan_used_up(read):
                raise
        return await handler(self._fallback(request))


__all__ = ["CodexQuotaFallbackMiddleware", "read_codex_account", "read_codex_quota"]
