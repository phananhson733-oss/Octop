"""Octop's harness adapter awaits producer closure before releasing turn locks.

The pinned harness manager forwards async generators without closing the inner
iterator on early exit. Keep its preparation/cancellation behavior but explicitly
propagate closure. Remove this adapter only after upstream preserves this contract.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any, cast

from octop_harness import HarnessAgentManager


class ClosingHarnessAgentManager(HarnessAgentManager):
    async def stream(self, agent_id: str, request: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        entry = self.get_agent(agent_id)
        prepared = await self._prepare_request(agent_id, request)
        async with aclosing(
            cast(AsyncGenerator[Any, None], entry.agent.stream(prepared, **kwargs))
        ) as chunks:
            async for chunk in chunks:
                yield chunk

    async def resume_hitl(
        self, agent_id: str, thread_id: str, decisions: list[dict[str, Any]], **kwargs: Any
    ) -> AsyncGenerator[Any, None]:
        entry = self.get_agent(agent_id)
        async with (
            self._cancellable_stream(agent_id, thread_id) as event,
            aclosing(
                cast(
                    AsyncGenerator[Any, None],
                    entry.agent.resume_hitl(thread_id, decisions, **kwargs),
                )
            ) as chunks,
        ):
            async for chunk in chunks:
                if event.is_set():
                    break
                yield chunk
