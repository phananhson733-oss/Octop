"""Dashboard HITL resume SSE must persist follow-up approvals in the pending store."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from octop.api.routers.chat.routes import iter_dashboard_hitl_resume_sse
from octop.infra.gateway.hitl.coordinator import HitlChannelCoordinator


def _parse_sse_chunks(raw: str) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        data_line = next(
            (line[6:] for line in block.split("\n") if line.startswith("data: ")),
            None,
        )
        if data_line is None:
            continue
        chunks.append(json.loads(data_line))
    return chunks


@pytest.mark.asyncio
async def test_dashboard_hitl_resume_registers_followup_hitl_required() -> None:
    async def _resume(*_args: object, **_kwargs: object):
        yield {"type": "token", "content": "ok"}
        yield {
            "type": "hitl_required",
            "request": {
                "action_requests": [
                    {"name": "execute", "args": {"command": "ls -la /private/etc"}},
                ],
                "review_configs": [
                    {"action_name": "execute", "allowed_decisions": ["approve", "reject"]},
                ],
            },
        }

    processor = MagicMock()
    processor.iter_hitl_resume_chunks = _resume
    hitl = HitlChannelCoordinator()
    first = hitl.store.register(
        thread_id="thr-follow",
        agent_id="agent-1",
        user_id=1,
        session_key="agent-1:dashboard:1:dm",
        channel_type="dashboard",
        action_requests=[{"name": "execute", "args": {"command": "ls -la /etc"}}],
        review_configs=None,
    )

    frames: list[str] = []
    async for frame in iter_dashboard_hitl_resume_sse(
        server=object(),
        processor=processor,
        hitl_coordinator=hitl,
        agent_id="agent-1",
        thread_id="thr-follow",
        user_id=1,
        decisions=[{"type": "approve"}],
        pending=first,
        session_key="agent-1:dashboard:1:dm",
        channel_type="dashboard",
        locale="zh",
        is_disconnected=AsyncMock(return_value=False),
    ):
        frames.append(frame)

    chunks = _parse_sse_chunks("".join(frames))
    hitl_chunks = [c for c in chunks if c.get("type") == "hitl_required"]
    assert hitl_chunks
    assert chunks[-1] == {"type": "done", "hitl_paused": True}

    followup = hitl.store.resolve_pending_for_thread(
        "thr-follow",
        agent_id="agent-1",
        user_id=1,
    )
    assert followup is not None
    assert hitl_chunks[0]["request"]["pending_id"] == followup.pending_id
    assert followup.pending_id != first.pending_id
    assert followup.action_requests[0]["args"]["command"] == "ls -la /private/etc"
    assert hitl.store.get(first.pending_id) is not None
    assert hitl.store.get(first.pending_id).status == "approved"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_dashboard_hitl_resume_wakes_team_member_without_host_resume() -> None:
    from types import SimpleNamespace

    hitl = HitlChannelCoordinator()
    pending = hitl.store.register(
        thread_id="thr-room",
        agent_id="host",
        user_id=1,
        session_key="host:dashboard:1:dm",
        channel_type="dashboard",
        action_requests=[{"name": "execute", "args": {"command": "df"}}],
        review_configs=None,
    )
    pending.resume_agent_id = "member"
    pending.resume_thread_id = "thr-room~member"
    delivered: dict[str, object] = {}

    def deliver(pending_id: str, decisions: list[dict[str, str]]) -> bool:
        delivered["id"] = pending_id
        delivered["decisions"] = decisions
        return True

    processor = SimpleNamespace(teams=SimpleNamespace(deliver_member_hitl=deliver))
    frames: list[str] = []
    async for frame in iter_dashboard_hitl_resume_sse(
        server=object(),
        processor=processor,
        hitl_coordinator=hitl,
        agent_id="host",
        thread_id="thr-room",
        user_id=1,
        decisions=[{"type": "approve"}],
        pending=pending,
        session_key="host:dashboard:1:dm",
        channel_type="dashboard",
        locale="zh",
        is_disconnected=AsyncMock(return_value=False),
    ):
        frames.append(frame)

    chunks = _parse_sse_chunks("".join(frames))
    assert chunks == [{"type": "done", "team_member_continuing": True}]
    assert delivered == {"id": pending.pending_id, "decisions": [{"type": "approve"}]}
    assert hitl.store.get(pending.pending_id) is not None
    assert hitl.store.get(pending.pending_id).status == "approved"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_dashboard_hitl_resume_expires_when_member_job_is_gone() -> None:
    from types import SimpleNamespace

    hitl = HitlChannelCoordinator()
    pending = hitl.store.register(
        thread_id="thr-room",
        agent_id="host",
        user_id=1,
        session_key="host:dashboard:1:dm",
        channel_type="dashboard",
        action_requests=[{"name": "execute", "args": {}}],
        review_configs=None,
    )
    pending.resume_agent_id = "member"
    pending.resume_thread_id = "thr-room~member"
    processor = SimpleNamespace(
        teams=SimpleNamespace(deliver_member_hitl=lambda *_args, **_kwargs: False)
    )
    frames: list[str] = []
    async for frame in iter_dashboard_hitl_resume_sse(
        server=object(),
        processor=processor,
        hitl_coordinator=hitl,
        agent_id="host",
        thread_id="thr-room",
        user_id=1,
        decisions=[{"type": "reject"}],
        pending=pending,
        session_key="host:dashboard:1:dm",
        channel_type="dashboard",
        locale="en",
        is_disconnected=AsyncMock(return_value=False),
    ):
        frames.append(frame)

    chunks = _parse_sse_chunks("".join(frames))
    assert chunks[0]["type"] == "error"
    assert "expired" in chunks[0]["message"].lower()
    assert hitl.store.get(pending.pending_id) is not None
    assert hitl.store.get(pending.pending_id).status == "expired"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_dashboard_hitl_resume_registers_without_prior_pending() -> None:
    async def _resume(*_args: object, **_kwargs: object):
        yield {
            "type": "hitl_required",
            "request": {
                "action_requests": [{"name": "execute", "args": {"command": "echo hi"}}],
            },
        }

    processor = MagicMock()
    processor.iter_hitl_resume_chunks = _resume
    hitl = HitlChannelCoordinator()

    async for _ in iter_dashboard_hitl_resume_sse(
        server=object(),
        processor=processor,
        hitl_coordinator=hitl,
        agent_id="agent-1",
        thread_id="thr-orphan",
        user_id=9,
        decisions=[{"type": "approve"}],
        pending=None,
        session_key="sk-orphan",
        channel_type="dashboard",
        locale="en",
        is_disconnected=AsyncMock(return_value=False),
    ):
        pass

    pending = hitl.store.resolve_pending_for_thread(
        "thr-orphan",
        agent_id="agent-1",
        user_id=9,
    )
    assert pending is not None
    assert pending.action_requests[0]["args"]["command"] == "echo hi"


@pytest.mark.asyncio
async def test_dashboard_hitl_resume_finishes_after_client_disconnect() -> None:
    completed = False

    async def _resume(*_args: object, **_kwargs: object):
        nonlocal completed
        yield {"type": "token", "content": "hidden after disconnect"}
        completed = True

    processor = MagicMock()
    processor.iter_hitl_resume_chunks = _resume
    hitl = HitlChannelCoordinator()

    frames = [
        frame
        async for frame in iter_dashboard_hitl_resume_sse(
            server=object(),
            processor=processor,
            hitl_coordinator=hitl,
            agent_id="agent-1",
            thread_id="thr-disconnected",
            user_id=9,
            decisions=[{"type": "respond", "message": "answer"}],
            pending=None,
            session_key="sk-disconnected",
            channel_type="dashboard",
            locale="en",
            is_disconnected=AsyncMock(return_value=True),
        )
    ]

    assert completed is True
    assert frames == []


@pytest.mark.asyncio
async def test_dashboard_hitl_resume_marks_pending_resolved_when_stream_errors() -> None:
    async def _resume(*_args: object, **_kwargs: object):
        raise RuntimeError("stream died")
        yield {}  # pragma: no cover

    processor = MagicMock()
    processor.iter_hitl_resume_chunks = _resume
    hitl = HitlChannelCoordinator()
    first = hitl.store.register(
        thread_id="thr-err",
        agent_id="agent-1",
        user_id=1,
        session_key="sk-err",
        channel_type="dashboard",
        action_requests=[{"name": "ask_user_question", "args": {"questions": []}}],
        review_configs=None,
    )

    frames: list[str] = []
    async for frame in iter_dashboard_hitl_resume_sse(
        server=object(),
        processor=processor,
        hitl_coordinator=hitl,
        agent_id="agent-1",
        thread_id="thr-err",
        user_id=1,
        decisions=[{"type": "respond", "message": "ok"}],
        pending=first,
        session_key="sk-err",
        channel_type="dashboard",
        locale="zh",
        is_disconnected=AsyncMock(return_value=False),
    ):
        frames.append(frame)

    chunks = _parse_sse_chunks("".join(frames))
    assert any(c.get("type") == "error" for c in chunks)
    resolved = hitl.store.get(first.pending_id)
    assert resolved is not None
    assert resolved.status == "expired"
    assert hitl.store.resolve_pending_for_thread("thr-err", agent_id="agent-1", user_id=1) is None


class _FakeHub:
    def __init__(self) -> None:
        self.active: set[str] = set()
        self.pushed: list[tuple[str, dict[str, Any]]] = []
        self.active_seen: list[bool] = []

    def mark_turn_active(self, thread_id: str) -> None:
        self.active.add(thread_id)

    def mark_turn_idle(self, thread_id: str) -> None:
        self.active.discard(thread_id)

    def is_turn_active(self, thread_id: str) -> bool:
        return thread_id in self.active

    async def push_to_thread(self, thread_id: str, frame: dict[str, Any]) -> None:
        self.pushed.append((thread_id, frame))


def _server_with_hub(hub: _FakeHub) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(app_runtime=SimpleNamespace(gateway=SimpleNamespace(ws_hub=hub)))


async def _start_resume(
    *,
    resume: Any,
    hub: _FakeHub,
    hitl: HitlChannelCoordinator,
    thread_id: str,
    pending: Any = None,
    received: list[dict[str, Any]] | None = None,
) -> tuple[asyncio.Task[None], list[dict[str, Any]], asyncio.Event]:
    """Consume the SSE like Starlette does; the returned task can be cancelled."""
    processor = MagicMock()
    processor.iter_hitl_resume_chunks = resume
    if received is None:
        received = []
    first = asyncio.Event()

    async def consume() -> None:
        async for frame in iter_dashboard_hitl_resume_sse(
            server=_server_with_hub(hub),
            processor=processor,
            hitl_coordinator=hitl,
            agent_id="agent-1",
            thread_id=thread_id,
            user_id=1,
            decisions=[{"type": "respond", "message": "不知道，先评"}],
            pending=pending,
            session_key="sk",
            channel_type="dashboard",
            locale="zh",
            is_disconnected=AsyncMock(return_value=False),
        ):
            received.extend(_parse_sse_chunks(frame))
            first.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(first.wait(), 2)
    return task, received, first


@pytest.mark.asyncio
async def test_dashboard_hitl_resume_survives_request_cancel_and_reaches_ws() -> None:
    """A reload cancels the SSE request; the resumed tool call must still finish."""
    tool_may_finish = asyncio.Event()
    finished = False

    async def _resume(*_args: object, **_kwargs: object):
        nonlocal finished
        yield {"type": "token", "content": "开始评了"}
        await tool_may_finish.wait()  # long tool call (content review)
        yield {"type": "tool_result", "content": "card"}
        yield {"type": "token", "content": "评完了"}
        finished = True

    hub = _FakeHub()
    hitl = HitlChannelCoordinator()
    task, received, _ = await _start_resume(
        resume=_resume, hub=hub, hitl=hitl, thread_id="thr-reload"
    )
    assert received == [{"type": "token", "content": "开始评了"}]
    # Attached, the resume belongs to that tab alone, as before.
    assert not hub.is_turn_active("thr-reload")
    job = hitl._resume_jobs["thr-reload"]

    task.cancel()  # what Starlette does on http.disconnect
    with pytest.raises(asyncio.CancelledError):
        await task
    # A reloaded page sees the turn as active and subscribes.
    assert hub.is_turn_active("thr-reload")
    tool_may_finish.set()
    assert job.task is not None
    await asyncio.wait_for(job.task, 2)

    assert finished is True
    assert [frame for _tid, frame in hub.pushed] == [
        {"type": "tool_result", "content": "card"},
        {"type": "token", "content": "评完了"},
        {"type": "turn.ended", "thread_id": "thr-reload"},
    ]
    assert not hub.is_turn_active("thr-reload")
    assert "thr-reload" not in hitl._resume_jobs


@pytest.mark.asyncio
async def test_dashboard_hitl_resume_attached_client_gets_everything_once() -> None:
    async def _resume(*_args: object, **_kwargs: object):
        yield {"type": "token", "content": "a"}
        yield {"type": "token", "content": "b"}

    hub = _FakeHub()
    hitl = HitlChannelCoordinator()
    task, received, _ = await _start_resume(
        resume=_resume, hub=hub, hitl=hitl, thread_id="thr-attached"
    )
    await asyncio.wait_for(task, 2)
    assert received == [
        {"type": "token", "content": "a"},
        {"type": "token", "content": "b"},
        {"type": "done"},
    ]
    assert hub.pushed == []
    assert not hub.is_turn_active("thr-attached")


@pytest.mark.asyncio
async def test_dashboard_hitl_resume_stop_cancels_detached_job() -> None:
    cancelled = False

    async def _resume(*_args: object, **_kwargs: object):
        nonlocal cancelled
        yield {"type": "token", "content": "x"}
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise
        yield {"type": "token", "content": "never"}  # pragma: no cover

    hub = _FakeHub()
    hitl = HitlChannelCoordinator()
    task, _received, _ = await _start_resume(
        resume=_resume, hub=hub, hitl=hitl, thread_id="thr-stop"
    )
    job = hitl._resume_jobs["thr-stop"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert hitl.cancel_resume("thr-stop") is True
    assert job.task is not None
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(job.task, 2)
    assert cancelled is True
    # Other tabs on the thread end the turn too, as with a stopped WS turn.
    assert [frame for _tid, frame in hub.pushed] == [
        {"type": "turn.ended", "thread_id": "thr-stop", "failed": True}
    ]
    assert not hub.is_turn_active("thr-stop")
    assert hitl.cancel_resume("thr-stop") is False


@pytest.mark.asyncio
async def test_dashboard_hitl_resume_detached_followup_card_parks_the_turn() -> None:
    go_on = asyncio.Event()

    async def _resume(*_args: object, **_kwargs: object):
        yield {"type": "token", "content": "x"}
        await go_on.wait()
        yield {
            "type": "hitl_required",
            "request": {"action_requests": [{"name": "ask_user_question", "args": {}}]},
        }

    hub = _FakeHub()
    hitl = HitlChannelCoordinator()
    task, _received, _ = await _start_resume(
        resume=_resume, hub=hub, hitl=hitl, thread_id="thr-park"
    )
    job = hitl._resume_jobs["thr-park"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    go_on.set()
    assert job.task is not None
    await asyncio.wait_for(job.task, 2)

    frames = [frame for _tid, frame in hub.pushed]
    assert frames[0]["type"] == "hitl_required"
    assert frames[-1] == {"type": "turn.ended", "thread_id": "thr-park", "hitl_paused": True}
    card = hitl.store.resolve_pending_for_thread("thr-park", agent_id="agent-1", user_id=1)
    assert card is not None
    assert frames[0]["request"]["pending_id"] == card.pending_id


@pytest.mark.asyncio
async def test_dashboard_hitl_resume_detached_error_ends_turn_failed() -> None:
    go_on = asyncio.Event()

    async def _resume(*_args: object, **_kwargs: object):
        yield {"type": "token", "content": "x"}
        await go_on.wait()
        raise RuntimeError("model down")

    hub = _FakeHub()
    hitl = HitlChannelCoordinator()
    first = hitl.store.register(
        thread_id="thr-fail",
        agent_id="agent-1",
        user_id=1,
        session_key="sk",
        channel_type="dashboard",
        action_requests=[{"name": "ask_user_question", "args": {"questions": []}}],
        review_configs=None,
    )
    task, _received, _ = await _start_resume(
        resume=_resume, hub=hub, hitl=hitl, thread_id="thr-fail", pending=first
    )
    job = hitl._resume_jobs["thr-fail"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    go_on.set()
    assert job.task is not None
    await asyncio.wait_for(job.task, 2)

    frames = [frame for _tid, frame in hub.pushed]
    assert frames[0]["type"] == "error"
    assert frames[-1] == {"type": "turn.ended", "thread_id": "thr-fail", "failed": True}
    resolved = hitl.store.get(first.pending_id)
    assert resolved is not None and resolved.status == "expired"


async def _until(check: Any) -> None:
    for _ in range(200):
        if check():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


def _job(
    *,
    resume: Any,
    hub: _FakeHub,
    hitl: HitlChannelCoordinator,
    thread_id: str,
    after_resume: Any = None,
) -> Any:
    from octop.infra.gateway.hitl.coordinator import HitlStreamContext
    from octop.infra.gateway.hitl.resume_job import HitlResumeJob

    processor = MagicMock()
    processor.iter_hitl_resume_chunks = resume
    return HitlResumeJob(
        processor=processor,
        hitl_coordinator=hitl,
        hub=hub,
        agent_id="agent-1",
        thread_id=thread_id,
        user_id=1,
        decisions=[],
        pending=None,
        hitl_ctx=HitlStreamContext(
            thread_id=thread_id,
            agent_id="agent-1",
            user_id=1,
            session_key="sk",
            channel_type="dashboard",
        ),
        format_error=str,
        after_resume=after_resume,
    )


@pytest.mark.parametrize("continued", [False, True])
@pytest.mark.asyncio
async def test_hitl_resume_client_left_before_done_job_finishes(continued: bool) -> None:
    """The stream ended but the client left before the route sent ``done``.

    The job runs the parked-steer continuation itself, and ends the turn on
    WS unless a continuation turn took over.
    """

    async def _resume(*_args: object, **_kwargs: object):
        yield {"type": "token", "content": "a"}

    hub = _FakeHub()
    hitl = HitlChannelCoordinator()
    calls: list[str] = []

    async def _continue() -> bool:
        calls.append("continuation")
        return continued

    job = _job(resume=_resume, hub=hub, hitl=hitl, thread_id="thr-left", after_resume=_continue)
    job.start()
    await asyncio.wait_for(job.task, 2)
    assert job.ended_ok
    job.detach()  # the route's ``finally`` after it saw the disconnect
    await _until(lambda: bool(calls) and (continued or bool(hub.pushed)))
    for _ in range(5):
        await asyncio.sleep(0)

    assert calls == ["continuation"]
    expected = [] if continued else [{"type": "turn.ended", "thread_id": "thr-left"}]
    assert [frame for _tid, frame in hub.pushed] == expected
    assert job.take_finish() is False


@pytest.mark.asyncio
async def test_hitl_resume_detached_continuation_keeps_turn_open() -> None:
    """Parked steers continue in a new WS turn after a detached resume.

    No ``turn.ended`` (the page would close its socket before the continuation),
    and the resume's hub release must not clear the continuation's active mark.
    """
    proceed = asyncio.Event()

    async def _resume(*_args: object, **_kwargs: object):
        yield {"type": "token", "content": "x"}
        await proceed.wait()
        yield {"type": "token", "content": "y"}

    hub = _FakeHub()
    hitl = HitlChannelCoordinator()
    seen_active: list[bool] = []

    async def _continue() -> bool:
        seen_active.append(hub.is_turn_active("thr-cont"))
        hub.mark_turn_active("thr-cont")  # the continuation turn starts
        return True

    job = _job(resume=_resume, hub=hub, hitl=hitl, thread_id="thr-cont", after_resume=_continue)
    job.start()
    chunks = job.chunks()
    assert await chunks.__anext__() == {"type": "token", "content": "x"}
    job.detach()
    assert hub.is_turn_active("thr-cont")
    proceed.set()
    await asyncio.wait_for(job.task, 2)

    assert seen_active == [False]
    assert hub.is_turn_active("thr-cont")
    assert [frame for _tid, frame in hub.pushed] == [{"type": "token", "content": "y"}]


@pytest.mark.asyncio
async def test_hitl_resume_second_resume_on_same_thread_is_refused() -> None:
    release = asyncio.Event()

    async def _resume(*_args: object, **_kwargs: object):
        yield {"type": "token", "content": "first"}
        await release.wait()

    hub = _FakeHub()
    hitl = HitlChannelCoordinator()
    task, _received, _ = await _start_resume(
        resume=_resume, hub=hub, hitl=hitl, thread_id="thr-dup"
    )
    first_job = hitl._resume_jobs["thr-dup"]

    second = MagicMock()
    second.iter_hitl_resume_chunks = MagicMock(side_effect=AssertionError("must not resume twice"))
    frames = [
        frame
        async for frame in iter_dashboard_hitl_resume_sse(
            server=_server_with_hub(hub),
            processor=second,
            hitl_coordinator=hitl,
            agent_id="agent-1",
            thread_id="thr-dup",
            user_id=1,
            decisions=[],
            pending=None,
            session_key="sk",
            channel_type="dashboard",
            locale="zh",
            is_disconnected=AsyncMock(return_value=False),
        )
    ]
    chunks = [chunk for frame in frames for chunk in _parse_sse_chunks(frame)]
    assert len(chunks) == 1 and chunks[0]["type"] == "error"
    assert hitl._resume_jobs["thr-dup"] is first_job

    release.set()
    await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_hitl_resume_attached_done_goes_out_before_the_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from octop.infra.gateway.ws import ws_channel

    async def _resume(*_args: object, **_kwargs: object):
        yield {"type": "token", "content": "x"}

    hub = _FakeHub()
    hitl = HitlChannelCoordinator()
    received: list[dict[str, Any]] = []
    seen_at_continuation: list[list[dict[str, Any]]] = []

    async def _fake_resume_parked_steers(*_args: object, **_kwargs: object) -> bool:
        seen_at_continuation.append(list(received))
        return True

    monkeypatch.setattr(ws_channel, "resume_parked_steers", _fake_resume_parked_steers)
    task, _, _ = await _start_resume(
        resume=_resume, hub=hub, hitl=hitl, thread_id="thr-order", received=received
    )
    await asyncio.wait_for(task, 2)
    assert received == [{"type": "token", "content": "x"}, {"type": "done"}]
    assert seen_at_continuation == [[{"type": "token", "content": "x"}, {"type": "done"}]]
    assert hub.pushed == []


@pytest.mark.asyncio
async def test_hitl_resume_stop_before_the_task_runs_cleans_up() -> None:
    async def _resume(*_args: object, **_kwargs: object):  # pragma: no cover
        yield {"type": "token", "content": "never"}

    hub = _FakeHub()
    hitl = HitlChannelCoordinator()
    job = _job(resume=_resume, hub=hub, hitl=hitl, thread_id="thr-early")
    job.start()
    assert hitl.cancel_resume("thr-early") is True
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(job.task, 2)
    await asyncio.sleep(0)
    assert hitl.resume_running("thr-early") is False
    # A client still attached is released instead of waiting forever.
    assert [chunk async for chunk in job.chunks()] == []


class _BlockingHub(_FakeHub):
    """Push of a chosen frame type waits until released."""

    def __init__(self, block_type: str) -> None:
        super().__init__()
        self.block_type = block_type
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()

    async def push_to_thread(self, thread_id: str, frame: dict[str, Any]) -> None:
        if frame.get("type") == self.block_type and not self.release.is_set():
            self.blocked.set()
            await self.release.wait()
        await super().push_to_thread(thread_id, frame)


@pytest.mark.asyncio
async def test_hitl_resume_stop_mid_push_saves_history_before_turn_ended() -> None:
    order: list[str] = []
    proceed = asyncio.Event()

    async def _resume(*_args: object, **_kwargs: object):
        try:
            yield {"type": "token", "content": "a"}
            await proceed.wait()
            yield {"type": "token", "content": "b"}
        finally:
            order.append("history saved")

    hub = _BlockingHub("token")
    hitl = HitlChannelCoordinator()
    job = _job(resume=_resume, hub=hub, hitl=hitl, thread_id="thr-save")
    job.start()
    chunks = job.chunks()
    assert await chunks.__anext__() == {"type": "token", "content": "a"}
    job.detach()
    proceed.set()
    await asyncio.wait_for(hub.blocked.wait(), 2)  # pushing "b" over WS

    hitl.cancel_resume("thr-save")
    await asyncio.sleep(0)
    hitl.cancel_resume("thr-save")  # a second click while history is saved
    hub.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(job.task, 2)
    order.extend(frame["type"] for _tid, frame in hub.pushed if frame["type"] == "turn.ended")
    assert order == ["history saved", "turn.ended"]


@pytest.mark.asyncio
async def test_hitl_resume_stop_after_the_stream_ended_does_not_cut_the_end() -> None:
    """Stop while the end is being delivered: the one ``turn.ended`` still goes out."""

    async def _resume(*_args: object, **_kwargs: object):
        yield {"type": "token", "content": "a"}

    hub = _BlockingHub("turn.ended")
    hitl = HitlChannelCoordinator()
    job = _job(resume=_resume, hub=hub, hitl=hitl, thread_id="thr-end")
    job.start()
    await asyncio.wait_for(job.task, 2)
    job.detach()  # client left before ``done``: the job finishes
    await asyncio.wait_for(hub.blocked.wait(), 2)

    hitl.cancel_resume("thr-end")
    hub.release.set()
    await _until(lambda: bool(hub.pushed))
    for _ in range(5):
        await asyncio.sleep(0)
    assert [frame for _tid, frame in hub.pushed] == [{"type": "turn.ended", "thread_id": "thr-end"}]


@pytest.mark.asyncio
async def test_hitl_resume_stop_during_the_continuation_sends_no_extra_end() -> None:
    """The continuation turn is already queued; Stop must not end it early."""
    in_continuation = asyncio.Event()
    may_return = asyncio.Event()

    async def _resume(*_args: object, **_kwargs: object):
        yield {"type": "token", "content": "a"}
        yield {"type": "token", "content": "b"}

    async def _continue() -> bool:
        in_continuation.set()
        await may_return.wait()  # the drain frames are being pushed
        return True

    hub = _FakeHub()
    hitl = HitlChannelCoordinator()
    job = _job(resume=_resume, hub=hub, hitl=hitl, thread_id="thr-cstop", after_resume=_continue)
    job.start()
    chunks = job.chunks()
    assert await chunks.__anext__() == {"type": "token", "content": "a"}
    job.detach()
    await asyncio.wait_for(in_continuation.wait(), 2)

    hitl.cancel_resume("thr-cstop")
    may_return.set()
    await asyncio.wait_for(job.task, 2)
    assert not job.task.cancelled()
    # "b" was queued for the client that left (dropped); no ``turn.ended``.
    assert hub.pushed == []
