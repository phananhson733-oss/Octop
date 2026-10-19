"""Agent turns that a dashboard page (a workbench plugin) starts in the background.

A plugin page cannot open the chat WebSocket. It posts a prompt instead; the turn runs the way a
scheduled agent task does (:class:`CronDeliveryService`), in a dashboard thread of its own: one
session per ``(agent, user, page session name)``, so the user's current chat thread is left alone,
the turn shows up in the chat sidebar, and the page polls the run for its outcome. Runs are kept in
memory only; after a restart the thread still has the history but the run id is gone.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from octop.infra.cron.delivery import CronDeliveryCommand, InteractionRequired
from octop.infra.gateway.threads import ThreadRegistry
from octop.infra.utils.ulid import new_ulid

if TYPE_CHECKING:
    from octop.infra.cron.delivery import CronDeliveryService
    from octop.infra.gateway.gateway import Gateway

logger = logging.getLogger(__name__)

PageRunStatus = Literal["running", "done", "needs_input", "failed"]
_KEEP_RUNS = 200
_REPLY_LIMIT = 8000


@dataclass
class PageRun:
    run_id: str
    agent_id: str
    user_id: int
    session: str
    session_key: str
    thread_id: str
    title: str
    started_at: float
    status: PageRunStatus = "running"
    finished_at: float | None = None
    reply: str = ""
    error: str = ""


def page_session_key(*, agent_id: str, user_id: int, session: str) -> str:
    """Session for page runs named *session*; never the user's dashboard DM session."""
    return ThreadRegistry.make_key(
        agent_id=agent_id,
        channel_type=ThreadRegistry.CHANNEL_DASHBOARD,
        channel_subject_id=str(user_id),
        channel_chat_type=f"page:{session}",
    )


class PageRuns:
    """Start page runs and remember how they ended."""

    def __init__(self, *, gateway: Gateway, delivery: CronDeliveryService) -> None:
        self._gateway = gateway
        self._delivery = delivery
        self._runs: dict[str, PageRun] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def get(self, run_id: str) -> PageRun | None:
        return self._runs.get(run_id)

    async def start(
        self,
        *,
        agent_id: str,
        user_id: int,
        session: str,
        prompt: str,
        title: str,
        fresh: bool = False,
    ) -> PageRun:
        """Start a run, or return the one still running under the same session name."""
        session_key = page_session_key(agent_id=agent_id, user_id=user_id, session=session)
        for run in self._runs.values():
            if run.session_key == session_key and run.status == "running":
                return run
        registry = self._gateway.thread_registry
        thread_id = await registry.get_or_create_by_key(
            session_key=session_key,
            agent_id=agent_id,
            user_id=user_id,
            channel_type=ThreadRegistry.CHANNEL_DASHBOARD,
        )
        if fresh:
            thread_id = await registry.reset_by_session_key(session_key)
        run = PageRun(
            run_id=new_ulid(),
            agent_id=agent_id,
            user_id=user_id,
            session=session,
            session_key=session_key,
            thread_id=thread_id,
            title=title,
            started_at=time.time(),
        )
        self._remember(run)
        task = asyncio.create_task(self._run(run, prompt))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run

    async def _run(self, run: PageRun, prompt: str) -> None:
        command = CronDeliveryCommand(
            cron_id=f"page:{run.run_id}",
            cron_name=run.title,
            agent_id=run.agent_id,
            user_id=run.user_id,
            session_key=run.session_key,
            prompt=prompt,
            fresh_thread=False,
            task_type="agent",
            model=None,
            mcp_servers=(),
            title=run.title,
        )
        try:
            reply = await self._delivery.deliver(command)
        except InteractionRequired:
            run.status = "needs_input"
        except Exception as exc:
            logger.exception("page run %s on agent %s failed", run.run_id, run.agent_id)
            run.status = "failed"
            run.error = str(exc)[:1000]
        else:
            run.status = "done"
            run.reply = (reply or "")[:_REPLY_LIMIT]
        finally:
            run.finished_at = time.time()

    def _remember(self, run: PageRun) -> None:
        self._runs[run.run_id] = run
        if len(self._runs) <= _KEEP_RUNS:
            return
        finished = [r for r in self._runs.values() if r.status != "running"]
        for old in sorted(finished, key=lambda r: r.started_at)[: len(self._runs) - _KEEP_RUNS]:
            del self._runs[old.run_id]


__all__ = ["PageRun", "PageRunStatus", "PageRuns", "page_session_key"]
