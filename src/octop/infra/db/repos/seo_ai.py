"""SQL persistence for AI visibility: engine settings, prompts, check runs and answers."""

from __future__ import annotations

import json
from typing import Any

from octop.infra.db.pool import DatabasePool

_RUN = "run_id,project_id,idempotency_key,status,started_at,heartbeat_at,payload"
_ANSWER = (
    "run_id,project_id,prompt_id,prompt,topic,engine,branded,status,answer_markdown,"
    "citations,brands,error,cost_usd,checked_at"
)
_JSON = {"citations", "brands"}


def _run(row: Any) -> dict[str, Any]:
    out = dict(row)
    out.update(json.loads(out.pop("payload")))
    return out


def _answer(row: Any) -> dict[str, Any]:
    out = dict(row)
    for column in _JSON & out.keys():
        out[column] = json.loads(out[column])
    out["branded"] = bool(out["branded"])
    return out


def _payload(run: dict[str, Any]) -> str:
    columns = set(_RUN.split(","))
    return json.dumps({k: v for k, v in run.items() if k not in columns}, ensure_ascii=False)


class SeoAiRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    # Settings and prompts --------------------------------------------------------

    def engines(self, project_id: str) -> list[str] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT engines FROM seo_ai_settings WHERE project_id=?", (project_id,)
            ).fetchone()
        return json.loads(row["engines"]) if row else None

    def put_engines(self, project_id: str, engines: list[str], now: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO seo_ai_settings(project_id,engines,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(project_id) DO UPDATE SET engines=excluded.engines,"
                "updated_at=excluded.updated_at",
                (project_id, json.dumps(engines), now),
            )

    def prompts(self, project_id: str) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT prompt_id,topic,prompt,created_at FROM seo_ai_prompts "
                "WHERE project_id=? ORDER BY id",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_prompts(
        self, project_id: str, prompts: list[dict[str, Any]], max_prompts: int
    ) -> int | None:
        """Add the prompts not saved yet; ``None`` (nothing added) when over the cap."""
        with self._db.transaction() as conn:
            saved = {
                row["prompt"]
                for row in conn.execute(
                    "SELECT prompt FROM seo_ai_prompts WHERE project_id=?", (project_id,)
                ).fetchall()
            }
            new = [prompt for prompt in prompts if prompt["prompt"] not in saved]
            if len(saved) + len(new) > max_prompts:
                return None
            for prompt in new:
                conn.execute(
                    "INSERT INTO seo_ai_prompts(prompt_id,project_id,topic,prompt,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        prompt["prompt_id"],
                        project_id,
                        prompt["topic"],
                        prompt["prompt"],
                        prompt["created_at"],
                    ),
                )
        return len(new)

    def remove_prompt(self, project_id: str, prompt_id: str) -> bool:
        """Stop asking a prompt; its answers stay with their checks."""
        with self._db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM seo_ai_prompts WHERE project_id=? AND prompt_id=?",
                (project_id, prompt_id),
            )
        return bool(cur.rowcount == 1)

    # Runs ------------------------------------------------------------------------

    @staticmethod
    def _settle(conn: Any, project_id: str, stale_before: int, now: str) -> None:
        # A run whose heartbeat stopped was cut off by a restart: an answer in flight
        # may have been charged without being saved, so its total cost is unknown.
        for row in conn.execute(
            f"SELECT {_RUN} FROM seo_ai_runs WHERE project_id=? AND status='running' "
            "AND heartbeat_at<?",
            (project_id, stale_before),
        ).fetchall():
            run = _run(row)
            run.update(status="unknown", error="interrupted", cost_usd=None, completed_at=now)
            conn.execute(
                "UPDATE seo_ai_runs SET status=?,payload=? WHERE run_id=?",
                (run["status"], _payload(run), run["run_id"]),
            )

    def settle(self, project_id: str, stale_before: int, now: str) -> None:
        with self._db.transaction() as conn:
            self._settle(conn, project_id, stale_before, now)

    def start_run(
        self, run: dict[str, Any], stale_before: int, now: str
    ) -> tuple[str, dict[str, Any] | None]:
        """Insert a running check: ``created``, ``replay`` (same key) or ``active`` (busy)."""
        with self._db.transaction() as conn:
            self._settle(conn, run["project_id"], stale_before, now)
            cur = conn.execute(
                f"INSERT INTO seo_ai_runs({_RUN}) VALUES(?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (
                    run["run_id"],
                    run["project_id"],
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
                f"SELECT {_RUN} FROM seo_ai_runs WHERE project_id=? AND idempotency_key=?",
                (run["project_id"], run["idempotency_key"]),
            ).fetchone()
        return ("replay", _run(row)) if row is not None else ("active", None)

    def save_run(self, run: dict[str, Any], answer: dict[str, Any] | None = None) -> bool:
        """Record one answer and the run's progress.

        False when the run is no longer running (settled as interrupted): the caller stops.
        """
        with self._db.transaction() as conn:
            if answer is not None:
                conn.execute(
                    f"INSERT INTO seo_ai_answers({_ANSWER}) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                    tuple(
                        json.dumps(answer[c], ensure_ascii=False)
                        if c in _JSON
                        else int(answer[c])
                        if c == "branded"
                        else answer[c]
                        for c in _ANSWER.split(",")
                    ),
                )
            cur = conn.execute(
                "UPDATE seo_ai_runs SET status=?,heartbeat_at=?,payload=? "
                "WHERE run_id=? AND status='running'",
                (run["status"], run["heartbeat_at"], _payload(run), run["run_id"]),
            )
        return bool(cur.rowcount == 1)

    def runs(self, project_id: str, limit: int) -> list[dict[str, Any]]:
        """The newest checks first."""
        with self._db.connect() as conn:
            rows = conn.execute(
                f"SELECT {_RUN} FROM seo_ai_runs WHERE project_id=? ORDER BY id DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [_run(row) for row in rows]

    def run(self, project_id: str, run_id: str) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                f"SELECT {_RUN} FROM seo_ai_runs WHERE project_id=? AND run_id=?",
                (project_id, run_id),
            ).fetchone()
        return _run(row) if row else None

    # Answers ---------------------------------------------------------------------

    def answers(self, run_id: str) -> list[dict[str, Any]]:
        """A check's answers in the order they were saved, without the answer text."""
        columns = _ANSWER.replace("answer_markdown,", "")
        with self._db.connect() as conn:
            rows = conn.execute(
                f"SELECT {columns} FROM seo_ai_answers WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        return [_answer(row) for row in rows]

    def answer(self, run_id: str, prompt_id: str, engine: str) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                f"SELECT {_ANSWER} FROM seo_ai_answers WHERE run_id=? AND prompt_id=? AND engine=?",
                (run_id, prompt_id, engine),
            ).fetchone()
        return _answer(row) if row else None
