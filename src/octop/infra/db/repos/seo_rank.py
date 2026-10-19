"""SQL persistence for rank trackers, their keywords, check runs and position snapshots."""

from __future__ import annotations

import json
from typing import Any

from octop.infra.db.pool import DatabasePool

_TRACKER = "tracker_id,project_id,domain,location_code,language_code,devices,depth,created_at"
_RUN = "run_id,tracker_id,idempotency_key,status,started_at,heartbeat_at,payload"
_SNAPSHOT = "keyword_id,keyword,device,position,url,serp_features,cost_usd,checked_at,run_id"


def _run(row: Any) -> dict[str, Any]:
    out = dict(row)
    out.update(json.loads(out.pop("payload")))
    return out


def _snapshot(row: Any) -> dict[str, Any]:
    out = dict(row)
    out["serp_features"] = json.loads(out["serp_features"])
    return out


def _payload(run: dict[str, Any]) -> str:
    columns = set(_RUN.split(","))
    return json.dumps({k: v for k, v in run.items() if k not in columns}, ensure_ascii=False)


class SeoRankRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    # Trackers --------------------------------------------------------------------

    def create_tracker(
        self, tracker: dict[str, Any], keywords: list[dict[str, Any]], max_trackers: int
    ) -> str:
        """Insert a tracker with its first keywords: ``created``, ``exists`` or ``full``."""
        with self._db.transaction() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM seo_rank_trackers WHERE project_id=?",
                (tracker["project_id"],),
            ).fetchone()["n"]
            if count >= max_trackers:
                return "full"
            cur = conn.execute(
                f"INSERT INTO seo_rank_trackers({_TRACKER}) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT DO NOTHING",
                tuple(tracker[column] for column in _TRACKER.split(",")),
            )
            if cur.rowcount != 1:
                return "exists"
            for keyword in keywords:
                conn.execute(
                    "INSERT INTO seo_rank_keywords(keyword_id,tracker_id,keyword,created_at) "
                    "VALUES(?,?,?,?)",
                    (
                        keyword["keyword_id"],
                        tracker["tracker_id"],
                        keyword["keyword"],
                        keyword["created_at"],
                    ),
                )
        return "created"

    def trackers(self, project_id: str) -> list[dict[str, Any]]:
        """Trackers oldest first, with how many keywords each has and when it last answered."""
        with self._db.connect() as conn:
            rows = conn.execute(
                f"SELECT {_TRACKER},"
                "(SELECT COUNT(*) FROM seo_rank_keywords k WHERE k.tracker_id=t.tracker_id) "
                "AS keyword_count,"
                "(SELECT MAX(checked_at) FROM seo_rank_snapshots s "
                "WHERE s.tracker_id=t.tracker_id) AS last_checked_at "
                "FROM seo_rank_trackers t WHERE project_id=? ORDER BY id",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def tracker(self, project_id: str, tracker_id: str) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                f"SELECT {_TRACKER} FROM seo_rank_trackers WHERE project_id=? AND tracker_id=?",
                (project_id, tracker_id),
            ).fetchone()
        return dict(row) if row else None

    def update_tracker(self, tracker_id: str, devices: str, depth: int) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE seo_rank_trackers SET devices=?,depth=? WHERE tracker_id=?",
                (devices, depth, tracker_id),
            )

    def delete_tracker(self, tracker_id: str) -> None:
        """Delete the tracker with its keywords, runs and snapshots."""
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM seo_rank_trackers WHERE tracker_id=?", (tracker_id,))

    # Keywords --------------------------------------------------------------------

    def keywords(self, tracker_id: str) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT keyword_id,keyword,created_at FROM seo_rank_keywords "
                "WHERE tracker_id=? ORDER BY id",
                (tracker_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_keywords(
        self, tracker_id: str, keywords: list[dict[str, Any]], max_keywords: int
    ) -> int | None:
        """Add the keywords not tracked yet; ``None`` (nothing added) when over the cap."""
        with self._db.transaction() as conn:
            tracked = {
                row["keyword"]
                for row in conn.execute(
                    "SELECT keyword FROM seo_rank_keywords WHERE tracker_id=?", (tracker_id,)
                ).fetchall()
            }
            new = [keyword for keyword in keywords if keyword["keyword"] not in tracked]
            if len(tracked) + len(new) > max_keywords:
                return None
            for keyword in new:
                conn.execute(
                    "INSERT INTO seo_rank_keywords(keyword_id,tracker_id,keyword,created_at) "
                    "VALUES(?,?,?,?)",
                    (keyword["keyword_id"], tracker_id, keyword["keyword"], keyword["created_at"]),
                )
        return len(new)

    def remove_keyword(self, tracker_id: str, keyword_id: str) -> bool:
        """Stop tracking a keyword; its snapshots stay as history."""
        with self._db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM seo_rank_keywords WHERE tracker_id=? AND keyword_id=?",
                (tracker_id, keyword_id),
            )
        return bool(cur.rowcount == 1)

    # Runs ------------------------------------------------------------------------

    @staticmethod
    def _settle(conn: Any, tracker_id: str, stale_before: int, now: str) -> None:
        # A run whose heartbeat stopped was cut off by a restart: a check in flight
        # may have been charged without an answer, so its total cost is unknown.
        for row in conn.execute(
            f"SELECT {_RUN} FROM seo_rank_runs WHERE tracker_id=? AND status='running' "
            "AND heartbeat_at<?",
            (tracker_id, stale_before),
        ).fetchall():
            run = _run(row)
            run.update(status="unknown", error="interrupted", cost_usd=None, completed_at=now)
            conn.execute(
                "UPDATE seo_rank_runs SET status=?,payload=? WHERE run_id=?",
                (run["status"], _payload(run), run["run_id"]),
            )

    def settle(self, tracker_id: str, stale_before: int, now: str) -> None:
        with self._db.transaction() as conn:
            self._settle(conn, tracker_id, stale_before, now)

    def start_run(
        self, run: dict[str, Any], stale_before: int, now: str
    ) -> tuple[str, dict[str, Any] | None]:
        """Insert a running check: ``created``, ``replay`` (same key) or ``active`` (busy)."""
        with self._db.transaction() as conn:
            self._settle(conn, run["tracker_id"], stale_before, now)
            cur = conn.execute(
                f"INSERT INTO seo_rank_runs({_RUN}) VALUES(?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (
                    run["run_id"],
                    run["tracker_id"],
                    run["idempotency_key"],
                    run["status"],
                    run["started_at"],
                    run["heartbeat_at"],
                    _payload(run),
                ),
            )
            if cur.rowcount == 1:
                return "created", run
            row = conn.execute(
                f"SELECT {_RUN} FROM seo_rank_runs WHERE tracker_id=? AND idempotency_key=?",
                (run["tracker_id"], run["idempotency_key"]),
            ).fetchone()
        return ("replay", _run(row)) if row is not None else ("active", None)

    def save_run(self, run: dict[str, Any], snapshot: dict[str, Any] | None = None) -> bool:
        """Record one check's answer and the run's progress.

        False when the run is no longer running (settled as interrupted): the caller stops.
        """
        with self._db.transaction() as conn:
            if snapshot is not None:
                conn.execute(
                    f"INSERT INTO seo_rank_snapshots(tracker_id,{_SNAPSHOT}) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                    (
                        run["tracker_id"],
                        *(
                            json.dumps(snapshot[c]) if c == "serp_features" else snapshot[c]
                            for c in _SNAPSHOT.split(",")
                        ),
                    ),
                )
            cur = conn.execute(
                "UPDATE seo_rank_runs SET status=?,heartbeat_at=?,payload=? "
                "WHERE run_id=? AND status='running'",
                (run["status"], run["heartbeat_at"], _payload(run), run["run_id"]),
            )
        return bool(cur.rowcount == 1)

    def latest_run(self, tracker_id: str) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                f"SELECT {_RUN} FROM seo_rank_runs WHERE tracker_id=? ORDER BY id DESC LIMIT 1",
                (tracker_id,),
            ).fetchone()
        return _run(row) if row else None

    # Snapshots -------------------------------------------------------------------

    def positions(
        self, tracker_id: str, before: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Per keyword and device: the newest snapshot, the newest before ``before``, the first.

        Snapshots of every run count, an interrupted one's too: each is a paid answer.
        """

        def pick(order: str, extra: str = "", args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
            rows = conn.execute(
                f"SELECT {_SNAPSHOT} FROM seo_rank_snapshots s WHERE tracker_id=? AND id=("
                "SELECT id FROM seo_rank_snapshots p WHERE p.tracker_id=s.tracker_id "
                f"AND p.keyword_id=s.keyword_id AND p.device=s.device{extra} "
                f"ORDER BY p.checked_at {order}, p.id {order} LIMIT 1)",
                (tracker_id, *args),
            ).fetchall()
            return [_snapshot(row) for row in rows]

        with self._db.connect() as conn:
            return (
                pick("DESC"),
                pick("DESC", " AND p.checked_at<?", (before,)),
                pick("ASC"),
            )

    def history(self, tracker_id: str, keyword_id: str, limit: int) -> list[dict[str, Any]]:
        """A keyword's newest ``limit`` snapshots, oldest first."""
        with self._db.connect() as conn:
            rows = conn.execute(
                f"SELECT {_SNAPSHOT} FROM seo_rank_snapshots WHERE tracker_id=? AND keyword_id=? "
                "ORDER BY checked_at DESC, id DESC LIMIT ?",
                (tracker_id, keyword_id, limit),
            ).fetchall()
        return [_snapshot(row) for row in reversed(rows)]
