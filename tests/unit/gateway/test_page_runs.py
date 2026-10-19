"""Background agent turns started from workbench pages (PageRuns + its routes)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from octop.infra.cron.delivery import CronDeliveryCommand, InteractionRequired
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.sessions import SessionRepo
from octop.infra.db.repos.threads import ThreadRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.errors import OctopError
from octop.infra.gateway.page_runs import PageRuns
from octop.infra.gateway.threads import ThreadRegistry


class FakeDelivery:
    """Holds each run until the test says how it ends."""

    def __init__(self) -> None:
        self.commands: list[CronDeliveryCommand] = []
        self.outcome: asyncio.Future[Any] | None = None

    async def deliver(self, command: CronDeliveryCommand) -> str | None:
        self.commands.append(command)
        self.outcome = asyncio.get_running_loop().create_future()
        result = await self.outcome
        if isinstance(result, BaseException):
            raise result
        return str(result)


@pytest.fixture
def registry(tmp_path: Path) -> ThreadRegistry:
    db = SqlitePool(tmp_path / "octop.db")
    run_migrations(db)
    UserRepo(db).create(username="u", password_hash="x", role="user")
    AgentRepo(db).create(agent_id="a1", user_id=1, name="Blog")
    return ThreadRegistry(session_repo=SessionRepo(db), thread_repo=ThreadRepo(db))


async def _settle(runs: PageRuns, delivery: FakeDelivery, result: Any) -> None:
    for _ in range(20):  # let the run's task reach deliver()
        if delivery.outcome is not None and not delivery.outcome.done():
            break
        await asyncio.sleep(0)
    assert delivery.outcome is not None
    delivery.outcome.set_result(result)
    for _ in range(20):
        await asyncio.sleep(0)
        if all(r.status != "running" for r in runs._runs.values()):
            return


async def test_a_page_run_gets_its_own_thread_and_leaves_the_chat_alone(
    registry: ThreadRegistry,
) -> None:
    chat_thread = await registry.get_or_create(
        agent_id="a1", user_id=1, channel_type="dashboard", channel_subject_id="1"
    )
    delivery = FakeDelivery()
    runs = PageRuns(gateway=SimpleNamespace(thread_registry=registry), delivery=delivery)  # type: ignore[arg-type]

    run, started = await runs.start(
        agent_id="a1", user_id=1, session="blog-x", prompt="按审稿意见改", title="改稿 x"
    )
    await asyncio.sleep(0)
    dm_key = ThreadRegistry.dashboard_key(agent_id="a1", user_id=1)
    assert started and run.session_key != dm_key
    assert registry.get_bound_thread_id(dm_key) == chat_thread
    assert run.thread_id not in (chat_thread, "")
    assert run.status == "running"
    # Named and listed in the sidebar before the agent has answered.
    thread = registry.get_thread(run.thread_id)
    assert thread is not None and thread.title == "改稿 x" and thread.last_active > 0
    # A second press while it runs gets the same run, not a second turn.
    again, started_again = await runs.start(
        agent_id="a1", user_id=1, session="blog-x", prompt="再来", title="t"
    )
    assert again is run and not started_again and len(delivery.commands) == 1
    sent = delivery.commands[0]
    assert (sent.session_key, sent.task_type, sent.title, sent.prompt, sent.live_history) == (
        run.session_key,
        "agent",
        "改稿 x",
        "按审稿意见改",
        True,
    )

    await _settle(runs, delivery, "改好了")
    assert (run.status, run.reply) == ("done", "改好了")
    assert run.finished_at is not None
    assert registry.get_bound_thread_id(dm_key) == chat_thread

    # Same name continues the same thread under the new task's title; fresh starts a new one.
    later, _ = await runs.start(
        agent_id="a1", user_id=1, session="blog-x", prompt="p", title="改写 x"
    )
    assert later.thread_id == run.thread_id
    later_thread = registry.get_thread(run.thread_id)
    assert later_thread is not None and later_thread.title == "改写 x"
    await _settle(runs, delivery, "ok")
    fresh, _ = await runs.start(
        agent_id="a1", user_id=1, session="blog-x", prompt="p", title="t", fresh=True
    )
    assert fresh.thread_id != run.thread_id
    await _settle(runs, delivery, "ok")
    # Fresh under a name never used before makes one thread, not an empty one plus another.
    first, _ = await runs.start(
        agent_id="a1", user_id=1, session="new-1", prompt="p", title="新写", fresh=True
    )
    threads = registry.list_threads_for_session(session_key=first.session_key)
    assert [t.thread_id for t in threads] == [first.thread_id]
    await _settle(runs, delivery, "ok")


async def test_a_question_to_the_user_and_a_failure_are_reported(registry: ThreadRegistry) -> None:
    delivery = FakeDelivery()
    runs = PageRuns(gateway=SimpleNamespace(thread_registry=registry), delivery=delivery)  # type: ignore[arg-type]

    asked, _ = await runs.start(agent_id="a1", user_id=1, session="new-1", prompt="p", title="t")
    await asyncio.sleep(0)
    await _settle(runs, delivery, InteractionRequired("cron agent run requires user interaction"))
    broke, _ = await runs.start(agent_id="a1", user_id=1, session="new-2", prompt="p", title="t")
    await asyncio.sleep(0)
    await _settle(runs, delivery, RuntimeError("model down"))

    assert asked.status == "needs_input"
    assert (broke.status, broke.error) == ("failed", "model down")
    assert runs.get(asked.run_id) is asked and runs.get("nope") is None


async def test_routes_are_for_the_owner_and_admins(registry: ThreadRegistry) -> None:
    from octop.api.routers.chat.page_runs import PageRunBody, get_page_run, start_page_run

    delivery = FakeDelivery()
    runs = PageRuns(gateway=SimpleNamespace(thread_registry=registry), delivery=delivery)  # type: ignore[arg-type]
    row = SimpleNamespace(agent_id="a1", user_id=1, is_shared=0, enabled=True, last_state="running")
    agent_registry = SimpleNamespace(get_row=lambda _aid: row, get_agent=lambda _aid: object())
    server = SimpleNamespace(
        app_runtime=SimpleNamespace(agent_registry=agent_registry, page_runs=runs)
    )
    body = PageRunBody(prompt="写一篇", session="new-abc", title="新写一篇")
    owner = SimpleNamespace(id=1, is_admin=False)
    other = SimpleNamespace(id=2, is_admin=False)
    admin = SimpleNamespace(id=9, is_admin=True)

    with pytest.raises(OctopError):
        await start_page_run("a1", body, user=other, server=server)
    started = await start_page_run("a1", body, user=owner, server=server)
    await asyncio.sleep(0)
    pressed_again = await start_page_run("a1", body, user=owner, server=server)

    assert started.status == "running" and started.thread_id and not started.reused
    assert pressed_again.reused and pressed_again.run_id == started.run_id
    assert (await get_page_run("a1", started.run_id, user=admin, server=server)).run_id == (
        started.run_id
    )
    for user, agent_id, run_id in (
        (other, "a1", started.run_id),
        (owner, "a2", started.run_id),
        (owner, "a1", "missing"),
    ):
        with pytest.raises(OctopError):
            await get_page_run(agent_id, run_id, user=user, server=server)
    await _settle(runs, delivery, "ok")
    with pytest.raises(ValueError):
        PageRunBody(prompt="p", session="Has Spaces", title="t")
