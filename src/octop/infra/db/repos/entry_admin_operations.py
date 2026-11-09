"""Unified-entry admin operations: one row per desired change, with its latest outcome."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import insert_returning_id, now_ts

STATES = ("pending", "applied", "partial", "rejected")


@dataclass(frozen=True)
class EntryAdminOperationRow:
    pk: int
    operation_id: str
    account: str
    instance: str
    user_id: int
    version: int
    desired: str
    state: str
    result: str | None
    readback: str | None
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: Any) -> EntryAdminOperationRow:
        return cls(
            pk=int(row["id"]),
            operation_id=str(row["operation_id"]),
            account=str(row["account"]),
            instance=str(row["instance"]),
            user_id=int(row["user_id"]),
            version=int(row["version"]),
            desired=str(row["desired"]),
            state=str(row["state"]),
            result=row["result"],
            readback=row["readback"],
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )


class EntryAdminOperationRepo:
    def __init__(self, db: DatabasePool):
        self._db = db

    def create_next(
        self, operation_id: str, account: str, instance: str, user_id: int, desired: str
    ) -> EntryAdminOperationRow:
        """Insert a pending operation one version above the account's latest.

        Two concurrent submits for one account can pick the same version; the loser fails on
        ``UNIQUE (instance, user_id, version)`` and is not recorded.
        """
        ts = now_ts()
        with self._db.transaction() as conn:
            latest = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM entry_admin_operations "
                "WHERE instance = ? AND user_id = ?",
                (instance, user_id),
            ).fetchone()
            version = int(latest["v"]) + 1
            insert_returning_id(
                conn,
                "INSERT INTO entry_admin_operations(operation_id, account, instance, user_id, "
                "version, desired, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (operation_id, account, instance, user_id, version, desired, ts, ts),
            )
        row = self.get(operation_id)
        assert row is not None
        return row

    def get(self, operation_id: str) -> EntryAdminOperationRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM entry_admin_operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        return EntryAdminOperationRow.from_row(row) if row else None

    def latest_for_account(self, account: str) -> EntryAdminOperationRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM entry_admin_operations WHERE account = ? ORDER BY id DESC LIMIT 1",
                (account,),
            ).fetchone()
        return EntryAdminOperationRow.from_row(row) if row else None

    def pending(
        self, limit: int, instances: Sequence[str], after: int = 0
    ) -> list[EntryAdminOperationRow]:
        """Pending operations for ``instances`` with ``id`` above ``after``, oldest first."""
        if not instances:
            return []
        marks = ", ".join("?" for _ in instances)
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM entry_admin_operations WHERE state = 'pending' AND id > ? "
                f"AND instance IN ({marks}) ORDER BY id LIMIT ?",
                (after, *instances, limit),
            ).fetchall()
        return [EntryAdminOperationRow.from_row(row) for row in rows]

    def record(
        self, operation_id: str, state: str, result: str | None, readback: str | None
    ) -> None:
        """Store a delivery outcome on a pending operation.

        A late delivery never reopens a settled one: partial and rejected only change through
        :meth:`requeue` (or a confirmed ``applied``), and applied stays applied.
        """
        if state not in STATES:
            raise ValueError("invalid operation state")
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE entry_admin_operations SET state = ?, result = ?, readback = ?, "
                "updated_at = ? WHERE operation_id = ? "
                "AND (state = 'pending' OR (state = 'partial' AND ? = 'applied'))",
                (state, result, readback, now_ts(), operation_id, state),
            )

    def requeue(self, operation_id: str) -> bool:
        """Put a partially failed operation back in the retry queue under the same ID."""
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE entry_admin_operations SET state = 'pending', updated_at = ? "
                "WHERE operation_id = ? AND state = 'partial'",
                (now_ts(), operation_id),
            )
            return bool(cursor.rowcount)
