"""Atomic lifecycle and indexed pending work for ephemeral entry sessions."""

from __future__ import annotations

from octop.infra.db.pool import DatabasePool


class EntrySessionRepo:
    def __init__(self, db: DatabasePool):
        self._db = db

    def create(self, key: str, payload: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO entry_sessions(session_key, payload) VALUES (?, ?)", (key, payload)
            )

    def get(self, key: str) -> tuple[str, str | None] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT state, payload FROM entry_sessions WHERE session_key = ?", (key,)
            ).fetchone()
        return (str(row["state"]), row["payload"]) if row else None

    def renew(self, key: str, payload: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE entry_sessions SET payload = ? WHERE session_key = ? AND state = 'active'",
                (payload, key),
            )

    def revoke(self, key: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE entry_sessions SET state = 'pending' WHERE session_key = ? AND state = 'active'",
                (key,),
            )

    def complete(self, key: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE entry_sessions SET state = 'done', payload = NULL WHERE session_key = ? AND state = 'pending'",
                (key,),
            )

    def pending(self, after: str, limit: int) -> list[str]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT session_key FROM entry_sessions WHERE state = 'pending' AND session_key > ? ORDER BY session_key LIMIT ?",
                (after, limit),
            ).fetchall()
        return [str(row["session_key"]) for row in rows]
