"""Background agent turns started from workbench pages (no WebSocket)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from octop.api.common.agent import require_agent_owner_row
from octop.api.common.workspace import require_running_agent
from octop.api.deps import current_user, get_server
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.page_runs import PageRun

router = APIRouter()


class PageRunBody(BaseModel):
    prompt: str = Field(
        ..., min_length=1, max_length=20000, description="Message sent to the agent"
    )
    session: str = Field(
        ...,
        pattern=r"^[a-z0-9][a-z0-9-]{0,79}$",
        description=(
            "Name the page picks for this line of work. Runs with the same name share one "
            "dashboard thread; a second start while one is running returns the running one "
            "with reused: true."
        ),
    )
    title: str = Field(
        ..., min_length=1, max_length=80, description="Thread title in the chat sidebar"
    )
    fresh: bool = Field(False, description="Start a new thread under this session name")


class PageRunOut(BaseModel):
    run_id: str
    agent_id: str
    session: str
    thread_id: str = Field(..., description="Open it at /chat/{agent_id}/{thread_id}")
    title: str
    status: Literal["running", "done", "needs_input", "failed"] = Field(
        ...,
        description="needs_input: the agent asked the user something; answer it in the thread",
    )
    started_at: float
    finished_at: float | None
    reply: str = Field(..., description="The agent's visible reply when done (truncated)")
    error: str
    reused: bool = Field(
        False,
        description=(
            "On start: true when a run under this session name was still going, so that run "
            "came back and this prompt was not sent"
        ),
    )


def _out(run: PageRun, *, reused: bool = False) -> PageRunOut:
    return PageRunOut(
        run_id=run.run_id,
        agent_id=run.agent_id,
        session=run.session,
        thread_id=run.thread_id,
        title=run.title,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        reply=run.reply,
        error=run.error,
        reused=reused,
    )


@router.post(
    "/agents/{agent_id}/chat/page-runs",
    status_code=202,
    summary="Run an agent turn in the background",
    description=(
        "For workbench pages, which cannot open the chat WebSocket. The turn runs like a "
        "scheduled agent task in a dashboard thread of its own (the caller's current chat "
        "thread is untouched). The thread gets the title and appears in the chat sidebar at "
        "once, and the turn's messages are written to it as they come. Only the agent's owner "
        "and admins; the agent must be running. Poll the run with GET; runs are kept in memory, "
        "so after a restart the GET is 404 and the thread shows how far the turn got."
    ),
    response_model=PageRunOut,
)
async def start_page_run(
    agent_id: str,
    body: PageRunBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> PageRunOut:
    require_agent_owner_row(agent_id, user=user, as_user=None, server=server)
    require_running_agent(server, agent_id)
    run, started = await server.app_runtime.page_runs.start(
        agent_id=agent_id,
        user_id=int(user.id),
        session=body.session,
        prompt=body.prompt,
        title=body.title,
        fresh=body.fresh,
    )
    return _out(run, reused=not started)


@router.get(
    "/agents/{agent_id}/chat/page-runs/{run_id}",
    summary="Read a background run",
    description="The caller's own runs only (admins see all).",
    response_model=PageRunOut,
)
async def get_page_run(
    agent_id: str,
    run_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> PageRunOut:
    run = server.app_runtime.page_runs.get(run_id)
    visible = run is not None and (run.user_id == user.id or bool(getattr(user, "is_admin", False)))
    if run is None or run.agent_id != agent_id or not visible:
        raise OctopError(ErrorCode.NOT_FOUND, "page run not found")
    return _out(run)
