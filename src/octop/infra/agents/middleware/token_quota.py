"""Reject agent turns when the acting user has exhausted their token quota."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langgraph.config import get_config

from octop.infra.users.resource_policy import (
    assert_monthly_budget_available,
    assert_token_quota_available,
)


def configurable_user_id() -> int | None:
    try:
        configurable = dict(get_config().get("configurable") or {})
    except RuntimeError:
        return None
    raw = configurable.get("user")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


class TokenQuotaMiddleware(AgentMiddleware[Any, Any]):
    """Check the acting user's tokens and spend this month before an agent turn."""

    def __init__(
        self,
        *,
        policy_repo: Any,
        usage_repo: Any,
        timezone: str = "UTC",
        prices: Callable[[], Mapping[str, tuple[float, float]]] = dict,
    ) -> None:
        super().__init__()
        self._policies = policy_repo
        self._usage = usage_repo
        self._timezone = timezone
        self._prices = prices

    def _enforce(self) -> None:
        user_id = configurable_user_id()
        if user_id is not None:
            assert_token_quota_available(
                self._policies, self._usage, user_id, timezone=self._timezone
            )
            assert_monthly_budget_available(
                self._policies,
                self._usage,
                user_id,
                prices=self._prices,
                timezone=self._timezone,
            )

    def before_agent(self, state: Any, runtime: Any) -> None:
        del state, runtime
        self._enforce()

    async def abefore_agent(self, state: Any, runtime: Any) -> None:
        del state, runtime
        self._enforce()


__all__ = ["TokenQuotaMiddleware", "configurable_user_id"]
