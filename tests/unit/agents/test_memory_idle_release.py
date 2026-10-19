"""Idle agents give back their memory database connections."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from octop_memory.storage.backends.postgres import PostgresMemoryBackend
from psycopg.pq import TransactionStatus

from octop.infra.agents.memory.idle_release import (
    IdleConnectionRelease,
    release_store_connection,
    running_agent_memories,
)

IDLE = 900.0


class FakeConn:
    def __init__(self) -> None:
        self.closed = False
        self.lock = threading.Lock()
        self.info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)

    def close(self) -> None:
        self.closed = True


class FakePool:
    def __init__(self) -> None:
        self.min_size = 1
        self.max_size = 4
        self.requests = 0
        self.checked_out = 0
        self.resizes: list[tuple[int, int]] = []

    def resize(self, min_size: int, max_size: int) -> None:
        self.resizes.append((min_size, max_size))
        self.min_size = min_size

    def get_stats(self) -> dict[str, int]:
        stats = {"pool_size": 1 + self.checked_out, "pool_available": 1}
        if self.requests:
            stats["requests_num"] = self.requests
        return stats


def memory(pool: FakePool | None = None) -> SimpleNamespace:
    backend = object.__new__(PostgresMemoryBackend)
    backend._closed = False
    backend._in_transaction = False
    backend._booting = False
    backend._conn = FakeConn()
    return SimpleNamespace(backend=backend, _checkpointer_pool=pool)


def test_releases_only_after_the_idle_window() -> None:
    quiet = memory()
    sweep = IdleConnectionRelease(lambda: [quiet], idle_seconds=IDLE)
    assert sweep.sweep(now=0) == 0
    assert sweep.sweep(now=IDLE - 1) == 0
    assert not quiet.backend._conn.closed
    assert sweep.sweep(now=IDLE) == 1
    assert quiet.backend._conn.closed
    # Already closed: nothing more to do until octop-memory reopens it.
    assert sweep.sweep(now=IDLE + 120) == 0
    quiet.backend._conn = FakeConn()
    # A connection just reopened is left one sweep, so a burst can finish its redial window.
    assert sweep.sweep(now=IDLE + 240) == 0
    assert sweep.sweep(now=IDLE + 360) == 1


def test_checkpointer_traffic_restarts_the_clock_and_lets_the_pool_drain() -> None:
    pool = FakePool()
    chatty = memory(pool)
    sweep = IdleConnectionRelease(lambda: [chatty, chatty], idle_seconds=IDLE)
    sweep.sweep(now=0)
    assert pool.resizes == [(0, 4)]
    pool.requests = 3
    assert sweep.sweep(now=IDLE) == 0
    assert sweep.sweep(now=2 * IDLE - 1) == 0
    assert sweep.sweep(now=2 * IDLE) == 1
    assert pool.resizes == [(0, 4)]


def test_a_checked_out_checkpointer_connection_counts_as_busy() -> None:
    pool = FakePool()
    working = memory(pool)
    sweep = IdleConnectionRelease(lambda: [working], idle_seconds=IDLE)
    sweep.sweep(now=0)
    pool.checked_out = 1
    assert sweep.sweep(now=IDLE) == 0
    pool.checked_out = 0
    assert sweep.sweep(now=IDLE + 1) == 0
    assert sweep.sweep(now=2 * IDLE + 1) == 1


@pytest.mark.parametrize("state", ["_closed", "_in_transaction", "_booting"])
def test_a_store_in_use_or_shut_on_purpose_is_left_alone(state: str) -> None:
    held = memory()
    setattr(held.backend, state, True)
    assert not release_store_connection(held)
    assert not held.backend._conn.closed


def test_a_connection_mid_statement_or_transaction_is_left_alone() -> None:
    locked = memory()
    locked.backend._conn.lock.acquire()
    assert not release_store_connection(locked)
    in_tx = memory()
    in_tx.backend._conn.info.transaction_status = TransactionStatus.INTRANS
    assert not release_store_connection(in_tx)
    assert not in_tx.backend._conn.lock.locked()
    assert not (locked.backend._conn.closed or in_tx.backend._conn.closed)


def test_other_backends_and_unknown_layouts_are_ignored() -> None:
    assert not release_store_connection(SimpleNamespace(backend=object()))
    bare = memory()
    del bare.backend._booting
    assert not release_store_connection(bare)


def test_running_agent_memories_skips_agents_without_memory() -> None:
    kept = object()
    entries = [SimpleNamespace(agent=SimpleNamespace(memory=m)) for m in (kept, None)]
    registry = SimpleNamespace(harness_manager=SimpleNamespace(list_agents=lambda: entries))
    assert running_agent_memories(registry) == [kept]
    assert running_agent_memories(SimpleNamespace(harness_manager=None)) == []


@pytest.mark.asyncio
async def test_the_background_sweep_survives_errors_and_stops_cleanly() -> None:
    calls = 0

    def memories() -> list[object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("registry busy")
        return []

    sweep = IdleConnectionRelease(memories, sweep_seconds=0.01)
    sweep.start()
    for _ in range(100):
        if calls >= 3:
            break
        await asyncio.sleep(0.01)
    await sweep.stop()
    assert calls >= 3
    stopped_at = calls
    await asyncio.sleep(0.05)
    assert calls == stopped_at
