"""Run a dashboard HITL resume as a server task, not inside the HTTP request.

The dashboard resumes an approval (or an ``ask_user_question`` answer) over an
SSE POST. When the resumed turn ran inside that response, a page reload or a
route change aborted the request and Starlette cancelled the turn mid-tool:
the tool result never reached the checkpoint and the reply was lost.

A :class:`HitlResumeJob` owns the resumed turn. While the SSE client is
attached it reads chunks from the job's queue and the route ends the turn the
way it always did (``done``, then the parked-steer continuation). Once the
client goes away the job keeps running:

* the thread is marked active on the hub, so a reloaded page subscribes;
* later chunks go to the thread's WebSocket subscribers, best effort;
* the end is guaranteed: ``turn.ended`` (or a continuation turn that ends
  itself), on which a page that joined mid-turn reloads the stored turn.

Chunks the SSE client had queued but not sent when it left are not replayed:
the stored turn is the source of truth and the reload shows them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from octop.infra.gateway.hitl.coordinator import HitlChannelCoordinator, HitlStreamContext
from octop.infra.gateway.hitl.format import normalize_hitl_request
from octop.infra.gateway.hitl.store import HitlPendingRecord

logger = logging.getLogger(__name__)

_END = object()

# The event loop keeps only weak references to tasks; a resume whose request
# is gone must not be collected mid-run.
_RUNNING: set[asyncio.Task[None]] = set()


def _keep(task: asyncio.Task[None]) -> asyncio.Task[None]:
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    return task


class HitlResumeJob:
    """One resumed HITL turn; outlives the SSE request that started it."""

    def __init__(
        self,
        *,
        processor: Any,
        hitl_coordinator: HitlChannelCoordinator,
        hub: Any,
        agent_id: str,
        thread_id: str,
        user_id: int,
        decisions: list[dict[str, Any]],
        pending: HitlPendingRecord | None,
        hitl_ctx: HitlStreamContext,
        format_error: Callable[[BaseException], str],
        after_resume: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self._processor = processor
        self._hitl = hitl_coordinator
        self._hub = hub
        self._agent_id = agent_id
        self._thread_id = thread_id
        self._user_id = user_id
        self._decisions = decisions
        self._pending = pending
        self._hitl_ctx = hitl_ctx
        self._format_error = format_error
        self._after_resume = after_resume
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._attached = True
        self._streaming = False
        self._hub_active = False
        self._ended = False
        # The stream finished cleanly while attached. Whoever takes the finish
        # (the route after it sent ``done``, or detach) runs the continuation.
        self._ended_ok = False
        self._finish_taken = False
        self._cancel_requested = False
        self._task: asyncio.Task[None] | None = None

    @property
    def thread_id(self) -> str:
        return self._thread_id

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    @property
    def ended_ok(self) -> bool:
        """The stream finished without a new pause or an error."""
        return self._ended_ok

    def start(self) -> None:
        self._task = _keep(asyncio.create_task(self._run(), name=f"hitl-resume:{self._thread_id}"))
        self._task.add_done_callback(self._on_task_done)
        # Stays the thread's Stop target until the task is done.
        self._hitl.track_resume_job(self)

    def take_finish(self) -> bool:
        """Claim the clean end (continuation, ``turn.ended``); True only once."""
        if not self._ended_ok or self._finish_taken:
            return False
        self._finish_taken = True
        return True

    def detach(self) -> None:
        """The SSE client is gone; the rest of the turn goes to WS."""
        if not self._attached:
            return
        self._attached = False
        self._queue.put_nowait(_END)
        if self._streaming:
            self._hub_active = self._hub_call("mark_turn_active")
        elif self.take_finish():
            # The client left before it sent ``done``: finish here.
            _keep(
                asyncio.create_task(
                    self._finish_detached(), name=f"hitl-resume-finish:{self._thread_id}"
                )
            )

    def cancel(self) -> None:
        """User pressed Stop: end the resumed turn.

        Only while the agent stream runs. Once it finished or the job is
        ending, cancelling would only cut the hand-off to a continuation turn
        or the one ``turn.ended`` the watching pages wait for.
        """
        # One Stop only: a second click must not cut the stream's cleanup.
        if self._ended or self._ended_ok or self._cancel_requested:
            return
        if self._task is not None and not self._task.done():
            self._cancel_requested = True
            self._task.cancel()

    async def chunks(self) -> AsyncIterator[dict[str, Any]]:
        """Chunks for the attached SSE client, until the stream ends."""
        while True:
            item = await self._queue.get()
            if item is _END:
                return
            yield item

    async def _run(self) -> None:
        self._streaming = True
        followup_pause = False
        try:
            # ``aclosing``: a Stop while a chunk is being pushed must let the
            # stream's own cleanup (it saves the partial turn) finish before
            # ``turn.ended`` makes the pages reload history.
            async with contextlib.aclosing(
                self._processor.iter_hitl_resume_chunks(
                    agent_id=self._agent_id,
                    thread_id=self._thread_id,
                    user_id=self._user_id,
                    decisions=self._decisions,
                )
            ) as stream:
                async for chunk in stream:
                    if isinstance(chunk, dict) and chunk.get("type") == "hitl_required":
                        followup_pause = True
                        request_payload = chunk.get("request")
                        if isinstance(request_payload, dict):
                            request_payload = normalize_hitl_request(request_payload)
                            record = self._hitl.register_from_request(
                                request_payload, ctx=self._hitl_ctx
                            )
                            request_payload["pending_id"] = record.pending_id
                            chunk["request"] = request_payload
                    await self._emit(chunk)
            # Idle before any continuation turn marks the thread active again.
            self._stop_streaming()
            # A new approval card is still pending. Draining parked steers now
            # would start a turn that expires that card, so keep the queue.
            if followup_pause:
                await self._end({"type": "done", "hitl_paused": True}, hitl_paused=True)
                return
            self._ended_ok = True
            if self._attached:
                self._ended = True
                self._queue.put_nowait(_END)
            elif self.take_finish():
                await self._finish_detached()
        except asyncio.CancelledError:
            logger.info("hitl resume cancelled thread=%s", self._thread_id)
            self._stop_streaming()
            if not self._ended:
                await self._end(None, failed=True)
            raise
        except Exception as exc:
            self._stop_streaming()
            # The pause was cleared before the resume started; a failed resume
            # must not leave the record looking like a successful decision.
            if self._pending is not None:
                self._hitl.store.mark_resolved(self._pending.pending_id, "expired")
            await self._end({"type": "error", "message": self._format_error(exc)}, failed=True)

    async def _finish_detached(self) -> None:
        continued = await self.continue_steers()
        self._ended = True
        if continued:
            # The continuation is a normal WS turn and sends its own end.
            return
        await self._push_turn_ended()

    async def continue_steers(self) -> bool:
        """Drain steers parked across the pause into a WS turn; True if one started."""
        if self._after_resume is None:
            return False
        try:
            return bool(await self._after_resume())
        except Exception:
            logger.exception("steer resume drain failed thread=%s", self._thread_id)
            return False

    async def _end(
        self, last: dict[str, Any] | None, *, failed: bool = False, hitl_paused: bool = False
    ) -> None:
        self._ended = True
        if self._attached:
            if last is not None:
                self._queue.put_nowait(last)
            self._queue.put_nowait(_END)
            return
        if last is not None and last.get("type") == "error":
            await self._push(last)
        await self._push_turn_ended(failed=failed, hitl_paused=hitl_paused)

    async def _push_turn_ended(self, *, failed: bool = False, hitl_paused: bool = False) -> None:
        # ``turn.ended`` (not ``done``) ends a joined live turn on the dashboard
        # and makes it reload the stored turn. Sent on Stop too, like a WS turn,
        # so every tab watching the thread ends it.
        frame: dict[str, Any] = {"type": "turn.ended", "thread_id": self._thread_id}
        if failed:
            frame["failed"] = True
        if hitl_paused:
            frame["hitl_paused"] = True
        await self._push(frame)

    def _on_task_done(self, _task: asyncio.Task[None]) -> None:
        self._stop_streaming()
        self._hitl.forget_resume_job(self)
        if self._ended:
            return
        # Stop landed before the task first ran, so ``_run`` never ended it.
        self._ended = True
        if self._attached:
            self._queue.put_nowait(_END)
        else:
            _keep(
                asyncio.create_task(
                    self._push_turn_ended(failed=True),
                    name=f"hitl-resume-end:{self._thread_id}",
                )
            )

    def _stop_streaming(self) -> None:
        self._streaming = False
        if self._hub_active:
            self._hub_active = False
            self._hub_call("mark_turn_idle")

    async def _emit(self, chunk: dict[str, Any]) -> None:
        if self._attached:
            self._queue.put_nowait(chunk)
            return
        await self._push(chunk)

    async def _push(self, frame: dict[str, Any]) -> None:
        push = getattr(self._hub, "push_to_thread", None)
        if push is None:
            return
        # Delivery failures must never stop the resumed turn.
        try:
            await push(self._thread_id, frame)
        except Exception:
            logger.exception("hitl resume push failed thread=%s (turn continues)", self._thread_id)

    def _hub_call(self, name: str) -> bool:
        fn = getattr(self._hub, name, None)
        if not callable(fn):
            return False
        with contextlib.suppress(Exception):
            fn(self._thread_id)
            return True
        return False


__all__ = ["HitlResumeJob"]
