"""Give back the database connections of agents nobody is using.

On a PostgreSQL control plane every running agent's memory (octop-memory) holds one
connection for its store for as long as the agent runs, and after its first turn a
checkpointer pool that never shrinks below one connection. Behind a session-mode
pooler each of those pins a server connection, so idle agents fill the pool.

This sweep lets both go once an agent has been quiet for ``IDLE_SECONDS``; octop-memory
reopens them on the next use: the store redials a closed connection and restores its
``search_path`` (logging a "closed by the server; reconnecting" warning), and the pool
opens a connection on demand. The first memory access after a quiet spell pays one
connect.

Only octop-memory attributes are touched, each behind ``getattr`` so a dependency
change degrades to doing nothing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

IDLE_SECONDS = 15 * 60
SWEEP_SECONDS = 120.0


@dataclass
class _Quiet:
    requests: int
    since: float
    # The store connection open at the previous sweep. Only a connection seen twice is
    # closed: one octop-memory just reopened may be mid-burst inside its 2 s redial
    # cooldown, where a second close would fail the next statement instead of redialling.
    conn: Any = None


def _checkpointer_activity(memory: Any) -> tuple[int, bool]:
    """``(requests served so far, connection checked out now)`` of the checkpointer pool.

    Lets an idle pool drain to zero: octop-memory opens it with ``min_size=1``, and
    psycopg_pool only closes connections above the minimum (one per ``max_idle``).
    """
    pool = getattr(memory, "_checkpointer_pool", None)
    if pool is None:
        return 0, False
    if pool.min_size != 0:
        pool.resize(0, pool.max_size)
    stats = pool.get_stats()
    return stats.get("requests_num", 0), stats["pool_size"] > stats["pool_available"]


def release_store_connection(memory: Any) -> bool:
    """Close the memory store's connection if nothing is using it right now."""
    from psycopg.pq import TransactionStatus

    try:
        from octop_memory.storage.backends.postgres import PostgresMemoryBackend
    except ImportError:
        return False

    backend = getattr(memory, "backend", None)
    if not isinstance(backend, PostgresMemoryBackend):
        return False
    # ``transaction()`` and schema bootstrap own the connection before any statement runs;
    # ``close()`` means the store was shut on purpose and must stay shut.
    if any(getattr(backend, name, True) for name in ("_closed", "_in_transaction", "_booting")):
        return False
    conn = getattr(backend, "_conn", None)
    if conn is None or conn.closed or not conn.lock.acquire(blocking=False):
        return False
    try:
        if conn.info.transaction_status != TransactionStatus.IDLE:
            return False
        conn.close()
    finally:
        conn.lock.release()
    return True


class IdleConnectionRelease:
    """Every ``sweep_seconds``, release the connections of agents idle for ``idle_seconds``.

    An agent counts as busy while its checkpointer pool serves requests: every agent turn
    writes checkpoints, whatever channel it came from.
    """

    def __init__(
        self,
        memories: Callable[[], Iterable[Any]],
        *,
        idle_seconds: float = IDLE_SECONDS,
        sweep_seconds: float = SWEEP_SECONDS,
    ) -> None:
        self._memories = memories
        self._idle_seconds = idle_seconds
        self._sweep_seconds = sweep_seconds
        self._quiet: dict[int, _Quiet] = {}
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="memory-idle-release")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_seconds)
            try:
                await asyncio.to_thread(self.sweep)
            except Exception:
                logger.warning("idle memory connection sweep failed", exc_info=True)

    def sweep(self, now: float | None = None) -> int:
        """One pass; returns how many store connections were closed."""
        now = time.monotonic() if now is None else now
        seen: dict[int, _Quiet] = {}
        released = 0
        for memory in self._memories():
            key = id(memory)
            if key in seen:
                continue
            requests, busy = _checkpointer_activity(memory)
            conn = getattr(getattr(memory, "backend", None), "_conn", None)
            quiet = self._quiet.get(key)
            if busy or quiet is None or quiet.requests != requests:
                quiet = _Quiet(requests, now)
            elif (
                quiet.conn is conn
                and now - quiet.since >= self._idle_seconds
                and release_store_connection(memory)
            ):
                released += 1
            quiet.conn = conn
            seen[key] = quiet
        self._quiet = seen
        if released:
            logger.info("released %d idle memory store connection(s)", released)
        return released


def running_agent_memories(registry: Any) -> list[Any]:
    """The ``Memory`` of every agent the harness currently runs."""
    manager = registry.harness_manager
    if manager is None:
        return []
    memories = (entry.agent.memory for entry in manager.list_agents())
    return [memory for memory in memories if memory is not None]
