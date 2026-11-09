"""Bounded durable retry pass; no raw entry token or user interaction required."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from octop.infra.auth.sessions import REVOCATION_RECEIPT_HEADER
from octop.infra.unified_entry.store import EntryStore


@dataclass(frozen=True)
class ReconcileResult:
    completed: int
    pending: int
    cursor: str


async def reconcile_revocations(
    store: EntryStore, clients: Mapping[str, httpx.AsyncClient], after: str = "", limit: int = 50
) -> ReconcileResult:
    loop = asyncio.get_running_loop()
    entries = await loop.run_in_executor(None, store.pending_revocations, after, limit)
    completed = 0
    for key, session in entries:
        if session is None or session.binding.instance not in clients:
            continue
        try:
            response = await clients[session.binding.instance].post(
                "/api/auth/session/revoke",
                headers={"Authorization": f"Bearer {session.backend_token}"},
                follow_redirects=False,
            )
        except httpx.RequestError:
            continue
        if response.status_code == 204 and response.headers.get(REVOCATION_RECEIPT_HEADER) == "1":
            await loop.run_in_executor(None, store.finish_pending, key)
            completed += 1
    return ReconcileResult(completed, len(entries) - completed, entries[-1][0] if entries else "")
