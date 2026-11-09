"""Deliver desired grants from the unified entry to an instance and confirm them by readback.

States (plan §3.2.3): ``pending`` until the instance's receipt and a matching readback arrive;
``applied`` then; ``partial`` when a component failed (an admin may retry it under the same
operation ID); ``rejected`` when the instance refused it (stale version, conflicting content,
missing or ineligible account) and retrying cannot help.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx

from octop.infra.db.repos.entry_admin_operations import (
    EntryAdminOperationRepo,
    EntryAdminOperationRow,
)
from octop.infra.utils.control_signature import control_headers

logger = logging.getLogger(__name__)
REJECTED_STATUSES = frozenset({400, 403, 404, 409, 422})


@dataclass(frozen=True)
class ControlTarget:
    """How the entry signs calls to one instance."""

    audience: str
    key: str = field(repr=False)


@dataclass(frozen=True)
class AdminOperation:
    operation_id: str
    account: str
    instance: str
    user_id: int
    version: int
    desired: dict[str, Any]
    state: str
    result: dict[str, Any] | None
    readback: dict[str, Any] | None

    @classmethod
    def from_row(cls, row: EntryAdminOperationRow) -> AdminOperation:
        return cls(
            row.operation_id,
            row.account,
            row.instance,
            row.user_id,
            row.version,
            json.loads(row.desired),
            row.state,
            json.loads(row.result) if row.result else None,
            json.loads(row.readback) if row.readback else None,
        )


class OperationConflict(ValueError):
    """An operation ID reused for another account or other content."""


def normalize_desired(role: str, templates: list[str], max_agents: int | None) -> dict[str, Any]:
    return {
        "role": role.strip(),
        "templates": list(dict.fromkeys(templates)),
        "max_agents": max_agents,
    }


async def signed_request(
    client: httpx.AsyncClient,
    target: ControlTarget,
    method: str,
    path: str,
    payload: Mapping[str, Any] | None = None,
) -> httpx.Response:
    body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    headers = control_headers(
        target.key, method=method, path=path, body=body, audience=target.audience
    )
    if payload is not None:
        headers["Content-Type"] = "application/json"
    return await client.request(method, path, content=body, headers=headers, follow_redirects=False)


def readback_matches(operation: AdminOperation, readback: Mapping[str, Any]) -> bool:
    record = readback.get("record") or {}
    return (
        readback.get("role") == operation.desired["role"]
        and readback.get("templates") == operation.desired["templates"]
        and readback.get("max_agents") == operation.desired["max_agents"]
        and record.get("operation_id") == operation.operation_id
        and record.get("version") == operation.version
        and record.get("state") == "applied"
    )


def _error_body(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        return {"status": response.status_code, **data}
    return {"status": response.status_code}


async def dispatch(
    repo: EntryAdminOperationRepo,
    operation: AdminOperation,
    client: httpx.AsyncClient,
    target: ControlTarget,
) -> AdminOperation:
    """Send one operation, read the account back, and record what the entry now knows."""
    path = f"/api/control/users/{operation.user_id}/grants"
    payload = {
        **operation.desired,
        "operation_id": operation.operation_id,
        "version": operation.version,
    }
    state, result, readback = "pending", None, None
    try:
        response = await signed_request(client, target, "PUT", path, payload)
    except httpx.RequestError:
        response, result = None, {"error": "instance unreachable"}
    if response is not None and response.status_code == 200:
        result = response.json()
        # A partial receipt settles the state; only an admin retry runs it again.
        if result.get("state") == "partial":
            state = "partial"
        try:
            back = await signed_request(client, target, "GET", path)
        except httpx.RequestError:
            back = None
        if back is not None and back.status_code == 200:
            readback = back.json()
            if result.get("state") == "applied" and readback_matches(operation, readback):
                state = "applied"
    elif response is not None and response.status_code in REJECTED_STATUSES:
        state, result = "rejected", _error_body(response)
    elif response is not None:
        result = _error_body(response)
    await asyncio.to_thread(
        repo.record,
        operation.operation_id,
        state,
        json.dumps(result, ensure_ascii=False) if result is not None else None,
        json.dumps(readback, ensure_ascii=False) if readback is not None else None,
    )
    row = await asyncio.to_thread(repo.get, operation.operation_id)
    assert row is not None
    return AdminOperation.from_row(row)


async def submit(
    repo: EntryAdminOperationRepo,
    *,
    account: str,
    instance: str,
    user_id: int,
    desired: dict[str, Any],
    operation_id: str | None,
    client: httpx.AsyncClient,
    target: ControlTarget,
) -> AdminOperation:
    """Record a new operation (or find the one this ID names) and deliver it once now."""
    operation_id = operation_id or uuid4().hex
    encoded = json.dumps(desired, sort_keys=True, ensure_ascii=False)
    existing = await asyncio.to_thread(repo.get, operation_id)
    if existing is None:
        try:
            existing = await asyncio.to_thread(
                repo.create_next, operation_id, account, instance, user_id, encoded
            )
        except Exception:
            # A concurrent submit won the operation ID or this account's next version.
            existing = await asyncio.to_thread(repo.get, operation_id)
            if existing is None:
                raise OperationConflict("concurrent change for this account; retry") from None
    if (
        existing.account != account
        or existing.instance != instance
        or existing.user_id != user_id
        or json.loads(existing.desired) != desired
    ):
        raise OperationConflict("operation id reused with other content")
    operation = AdminOperation.from_row(existing)
    if operation.state != "pending":
        return operation
    return await dispatch(repo, operation, client, target)


@dataclass(frozen=True)
class AdminPass:
    applied: int
    # Resume after this row next pass; 0 starts over, so no batch of failing rows starves
    # the ones behind it.
    cursor: int


async def reconcile_admin_operations(
    repo: EntryAdminOperationRepo,
    clients: Mapping[str, httpx.AsyncClient],
    targets: Mapping[str, ControlTarget],
    cursor: int = 0,
    limit: int = 20,
) -> AdminPass:
    """Redeliver one batch of pending operations under their own IDs."""
    instances = sorted(set(clients) & set(targets))
    rows = await asyncio.to_thread(repo.pending, limit, instances, cursor)
    applied = 0
    for row in rows:
        # The batch may be stale: an admin request can settle a row before its turn.
        current = await asyncio.to_thread(repo.get, row.operation_id)
        if current is None or current.state != "pending":
            continue
        try:
            done = await dispatch(
                repo,
                AdminOperation.from_row(current),
                clients[row.instance],
                targets[row.instance],
            )
        except Exception:
            # Never log instance responses: they can carry account details.
            logger.warning("Entry admin operation delivery failed; it stays pending")
            continue
        applied += done.state == "applied"
    return AdminPass(applied, rows[-1].pk if len(rows) == limit else 0)
