"""A released memory connection and a drained checkpointer pool come back on next use."""

from __future__ import annotations

import os
import uuid

import pytest

from tests.support.postgresql import requires_postgresql


@requires_postgresql
@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_released_connections_reopen_on_next_use() -> None:
    from langgraph.graph import END, START, MessagesState, StateGraph
    from octop_memory import Memory

    from octop.infra.agents.memory.idle_release import IdleConnectionRelease

    memory = Memory(
        namespace=f"idle_{uuid.uuid4().hex[:12]}",
        backend="postgres",
        backend_config={"dsn": os.environ["OCTOP_TEST_DATABASE_URL"]},
    )
    builder = StateGraph(MessagesState)
    builder.add_node("answer", lambda state: {"messages": [("ai", "saved answer")]})
    builder.add_edge(START, "answer")
    builder.add_edge("answer", END)
    graph = builder.compile(checkpointer=memory)
    thread = {"configurable": {"thread_id": f"idle-{uuid.uuid4().hex}"}}
    sweep = IdleConnectionRelease(lambda: [memory], idle_seconds=60)
    try:
        await graph.ainvoke({"messages": [("human", "first")]}, thread)
        backend = memory.backend
        before = backend.get_tree()

        sweep.sweep(now=0)
        assert sweep.sweep(now=60) == 1
        assert backend._conn.closed
        pool = memory._checkpointer_pool
        assert pool.min_size == 0
        # psycopg_pool's shrink task closes one idle connection per ``max_idle``.
        for _ in range(pool.max_size):
            pool._shrink_pool()
        assert pool.get_stats()["pool_size"] == 0

        # The store redials with its search_path; the pool opens a connection on demand.
        assert backend.get_tree() == before
        assert not backend._conn.closed
        assert not backend._closed
        search_path = backend._conn.execute("SHOW search_path").fetchone()["search_path"]
        assert search_path.startswith("octop_memory")
        await graph.ainvoke({"messages": [("human", "second")]}, thread)
        state = await graph.aget_state(thread)
        assert [m.content for m in state.values["messages"]][-2:] == ["second", "saved answer"]
    finally:
        await memory.adelete_thread(thread["configurable"]["thread_id"])
        memory.backend.purge_namespace()
        memory.backend.close()
