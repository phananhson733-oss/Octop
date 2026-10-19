"""SQL persistence for Google sign-in grants and Search Console property bindings."""

from __future__ import annotations

from typing import Any

from octop.infra.db.pool import DatabasePool

_GRANT = "grant_id,user_id,google_account_id,email,scopes,credentials,created_at,updated_at"


def _grant(row: Any) -> dict[str, Any]:
    out = dict(row)
    out["scopes"] = [scope for scope in str(out["scopes"]).split() if scope]
    return out


class SeoGoogleRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create_state(self, value: dict[str, Any], expired_before: int) -> None:
        with self._db.transaction() as conn:
            # Abandoned consents never come back to delete their state.
            conn.execute("DELETE FROM seo_google_states WHERE created_at<?", (expired_before,))
            conn.execute(
                "INSERT INTO seo_google_states(state,user_id,code_verifier,redirect_uri,"
                "return_path,created_at) VALUES(?,?,?,?,?,?)",
                (
                    value["state"],
                    value["user_id"],
                    value["code_verifier"],
                    value["redirect_uri"],
                    value["return_path"],
                    value["created_at"],
                ),
            )

    def consume_state(self, state: str) -> dict[str, Any] | None:
        """Delete and return the state; of two callbacks presenting it, only one gets it."""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT user_id,code_verifier,redirect_uri,return_path,created_at "
                "FROM seo_google_states WHERE state=?",
                (state,),
            ).fetchone()
            if row is None:
                return None
            cur = conn.execute("DELETE FROM seo_google_states WHERE state=?", (state,))
            return dict(row) if cur.rowcount == 1 else None

    def save_grant(self, value: dict[str, Any]) -> str:
        """Insert or refresh the grant for (user, Google account); returns its grant_id."""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT grant_id FROM seo_google_grants WHERE user_id=? AND google_account_id=?",
                (value["user_id"], value["google_account_id"]),
            ).fetchone()
            if row is None:
                conn.execute(
                    f"INSERT INTO seo_google_grants({_GRANT}) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        value["grant_id"],
                        value["user_id"],
                        value["google_account_id"],
                        value["email"],
                        " ".join(value["scopes"]),
                        value["credentials"],
                        value["created_at"],
                        value["updated_at"],
                    ),
                )
                return str(value["grant_id"])
            conn.execute(
                "UPDATE seo_google_grants SET email=?,scopes=?,credentials=?,updated_at=? "
                "WHERE grant_id=?",
                (
                    value["email"],
                    " ".join(value["scopes"]),
                    value["credentials"],
                    value["updated_at"],
                    row["grant_id"],
                ),
            )
            return str(row["grant_id"])

    def grants(self, user_id: int) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute(
                f"SELECT {_GRANT} FROM seo_google_grants WHERE user_id=? ORDER BY id",
                (user_id,),
            ).fetchall()
        return [_grant(row) for row in rows]

    def grant(self, user_id: int, grant_id: str) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                f"SELECT {_GRANT} FROM seo_google_grants WHERE user_id=? AND grant_id=?",
                (user_id, grant_id),
            ).fetchone()
        return _grant(row) if row is not None else None

    def update_credentials(self, grant_id: str, credentials: str, updated_at: int) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE seo_google_grants SET credentials=?,updated_at=? WHERE grant_id=?",
                (credentials, updated_at, grant_id),
            )

    def delete_grant(self, user_id: int, grant_id: str) -> bool:
        """Remove the grant; projects bound through it lose their property too."""
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM seo_gsc_connections WHERE grant_id=?", (grant_id,))
            cur = conn.execute(
                "DELETE FROM seo_google_grants WHERE user_id=? AND grant_id=?",
                (user_id, grant_id),
            )
            return bool(cur.rowcount == 1)

    def set_gsc(self, value: dict[str, Any]) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "DELETE FROM seo_gsc_connections WHERE project_id=?", (value["project_id"],)
            )
            conn.execute(
                "INSERT INTO seo_gsc_connections(project_id,grant_id,site_url,permission_level,"
                "connected_at) VALUES(?,?,?,?,?)",
                (
                    value["project_id"],
                    value["grant_id"],
                    value["site_url"],
                    value["permission_level"],
                    value["connected_at"],
                ),
            )

    def gsc(self, project_id: str) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT c.project_id,c.grant_id,c.site_url,c.permission_level,c.connected_at,"
                "g.email FROM seo_gsc_connections c JOIN seo_google_grants g "
                "ON g.grant_id=c.grant_id WHERE c.project_id=?",
                (project_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def delete_gsc(self, project_id: str) -> bool:
        with self._db.transaction() as conn:
            cur = conn.execute("DELETE FROM seo_gsc_connections WHERE project_id=?", (project_id,))
            return bool(cur.rowcount == 1)
