"""SQL persistence for native SEO evidence; saves and tags share a transaction."""

from __future__ import annotations

import json
import time
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.utils.ulid import new_ulid


class SeoRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create_project(self, user_id: int, value: dict[str, Any]) -> bool:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO seo_projects(project_id,user_id,agent_id,name,domain,location_code,"
                "language_code,created_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(agent_id) DO NOTHING",
                (
                    value["project_id"],
                    user_id,
                    value["agent_id"],
                    value["name"],
                    value["domain"],
                    value["location_code"],
                    value["language_code"],
                    value["created_at"],
                ),
            )
            return bool(cur.rowcount == 1)

    def projects(self, user_id: int) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT project_id,agent_id,name,domain,location_code,language_code,"
                "created_at FROM seo_projects WHERE user_id=? ORDER BY id DESC",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def start_run(self, run: dict[str, Any], key: str, input_hash: str) -> bool:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO seo_runs(run_id,project_id,idempotency_key,input_hash,"
                "created_at,status,payload) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(project_id,idempotency_key) DO NOTHING",
                (
                    run["run_id"],
                    run["project_id"],
                    key,
                    input_hash,
                    int(time.time()),
                    run["status"],
                    json.dumps(run),
                ),
            )
            return bool(cur.rowcount == 1)

    def run_for_key(self, project_id: str, key: str) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT input_hash,payload FROM seo_runs WHERE project_id=? AND idempotency_key=?",
                (project_id, key),
            ).fetchone()
        return {"input_hash": row["input_hash"], "run": json.loads(row["payload"])} if row else None

    def cached_run(self, project_id: str, input_hash: str) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM seo_runs WHERE project_id=? AND input_hash=? "
                "AND status='completed' AND created_at>? ORDER BY id DESC LIMIT 1",
                (project_id, input_hash, int(time.time()) - 86400),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def update_run(self, run: dict[str, Any]) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE seo_runs SET status=?,payload=? WHERE run_id=?",
                (run["status"], json.dumps(run), run["run_id"]),
            )

    def runs(self, project_id: str) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM seo_runs WHERE project_id=? ORDER BY id DESC", (project_id,)
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def call_start(self, call: dict[str, Any]) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO seo_calls(call_id,run_id,source,seed,status,created_at) "
                "VALUES(?,?,?,?,'started',?)",
                (call["call_id"], call["run_id"], call["source"], call["seed"], call["created_at"]),
            )

    def call_finish(
        self,
        call_id: str,
        status: str,
        cost: float | None,
        error: str | None,
        receipt: dict[str, Any],
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE seo_calls SET status=?,cost_usd=?,error=?,receipt_json=? WHERE call_id=?",
                (status, cost, error, json.dumps(receipt), call_id),
            )

    def calls(self, run_id: str) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT call_id,source,seed,status,cost_usd,error,created_at,receipt_json "
                "FROM seo_calls WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [
            {k: v for k, v in dict(row).items() if k != "receipt_json"}
            | {"receipt": json.loads(row["receipt_json"])}
            for row in rows
        ]

    def save_keywords(
        self, project_id: str, rows: list[dict[str, Any]], tags: list[str], replace: bool
    ) -> dict[str, int]:
        created = 0
        with self._db.transaction() as conn:
            # Serialize tags and keyword changes within a project on PostgreSQL.
            conn.execute(
                "SELECT project_id FROM seo_projects WHERE project_id=?"
                + (" FOR UPDATE" if self._db.dialect == "postgresql" else ""),
                (project_id,),
            ).fetchone()
            canonical_tags = []
            seen_tags: set[str] = set()
            for name in tags:
                normalized = name.lower()
                if normalized in seen_tags:
                    continue
                seen_tags.add(normalized)
                conn.execute(
                    "INSERT INTO seo_tags(project_id,normalized_name,name) VALUES(?,?,?) "
                    "ON CONFLICT(project_id,normalized_name) DO NOTHING",
                    (project_id, normalized, name),
                )
                tag = conn.execute(
                    "SELECT name FROM seo_tags WHERE project_id=? AND normalized_name=?",
                    (project_id, normalized),
                ).fetchone()
                canonical_tags.append(str(tag["name"]))
            # Acquire unique-row locks in a stable order across concurrent save batches.
            for value in sorted(
                rows, key=lambda row: (row["keyword"], row["location_code"], row["language_code"])
            ):
                row = dict(value)
                # ON CONFLICT handles concurrent PostgreSQL requests as well as SQLite.
                keyword_id = new_ulid()
                cur = conn.execute(
                    "INSERT INTO seo_keywords(keyword_id,project_id,keyword,"
                    "location_code,language_code,payload) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(project_id,keyword,location_code,language_code) DO NOTHING",
                    (
                        keyword_id,
                        project_id,
                        row["keyword"],
                        row["location_code"],
                        row["language_code"],
                        "{}",
                    ),
                )
                created += int(cur.rowcount == 1)
                existing = conn.execute(
                    "SELECT keyword_id,payload FROM seo_keywords "
                    "WHERE project_id=? AND keyword=? AND location_code=? "
                    "AND language_code=?"
                    + (" FOR UPDATE" if self._db.dialect == "postgresql" else ""),
                    (project_id, row["keyword"], row["location_code"], row["language_code"]),
                ).fetchone()
                old = json.loads(existing["payload"])
                row["keyword_id"] = existing["keyword_id"]
                row["tags"] = (
                    canonical_tags
                    if replace
                    else sorted(set(old.get("tags", [])) | set(canonical_tags))
                )
                conn.execute(
                    "UPDATE seo_keywords SET payload=? WHERE keyword_id=?",
                    (json.dumps(row), existing["keyword_id"]),
                )
        return {
            "saved_count": len(rows),
            "created_count": created,
            "updated_count": len(rows) - created,
        }

    def keywords(self, project_id: str) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM seo_keywords WHERE project_id=? ORDER BY id", (project_id,)
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_report(self, metadata: dict[str, Any], html: str, context: dict[str, Any]) -> bool:
        with self._db.transaction() as conn:
            conn.execute(
                "SELECT project_id FROM seo_projects WHERE project_id=?"
                + (" FOR UPDATE" if self._db.dialect == "postgresql" else ""),
                (metadata["project_id"],),
            ).fetchone()
            current_keywords = conn.execute(
                "SELECT payload FROM seo_keywords WHERE project_id=? ORDER BY id",
                (metadata["project_id"],),
            ).fetchall()
            if [json.loads(row["payload"]) for row in current_keywords] != context[
                "saved_keywords"
            ]:
                return False
            conn.execute(
                "INSERT INTO seo_reports(report_id,project_id,run_id,metadata,html,context_json) "
                "VALUES(?,?,?,?,?,?)",
                (
                    metadata["report_id"],
                    metadata["project_id"],
                    metadata["run_id"],
                    json.dumps(metadata),
                    html,
                    json.dumps(context),
                ),
            )
        return True

    def reports(self, project_id: str) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT metadata FROM seo_reports WHERE project_id=? ORDER BY id DESC",
                (project_id,),
            ).fetchall()
        return [json.loads(row["metadata"]) for row in rows]

    def report(self, project_id: str, report_id: str) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT metadata,html FROM seo_reports WHERE project_id=? AND report_id=?",
                (project_id, report_id),
            ).fetchone()
        return {**json.loads(row["metadata"]), "html": row["html"]} if row else None
