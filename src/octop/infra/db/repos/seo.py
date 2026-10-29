"""SQL persistence for native SEO evidence; saves and tags share a transaction."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.utils.ulid import new_ulid


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    """The run without its keyword rows, which is all a listing needs."""
    return {k: v for k, v in run.items() if k not in {"results", "calls"}} | {
        "results": [
            {k: v for k, v in result.items() if k != "rows"} | {"row_count": len(result["rows"])}
            for result in run["results"]
        ]
    }


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

    def set_agent(self, user_id: int, project_id: str, agent_id: str) -> str | None:
        """Bind ``agent_id``; returns the previous agent id, or ``"conflict"`` if it is taken."""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT agent_id FROM seo_projects WHERE project_id=? AND user_id=?"
                + (" FOR UPDATE" if self._db.dialect == "postgresql" else ""),
                (project_id, user_id),
            ).fetchone()
            taken = conn.execute(
                "SELECT project_id FROM seo_projects WHERE agent_id=? AND project_id<>?",
                (agent_id, project_id),
            ).fetchone()
            if row is None or taken is not None:
                return "conflict"
            conn.execute(
                "UPDATE seo_projects SET agent_id=? WHERE project_id=?", (agent_id, project_id)
            )
            return str(row["agent_id"] or "")

    def start_run(self, run: dict[str, Any], key: str, input_hash: str) -> bool:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO seo_runs(run_id,project_id,idempotency_key,input_hash,"
                "created_at,status,payload,summary) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(project_id,idempotency_key) DO NOTHING",
                (
                    run["run_id"],
                    run["project_id"],
                    key,
                    input_hash,
                    int(time.time()),
                    run["status"],
                    json.dumps(run),
                    json.dumps(_run_summary(run)),
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
                "UPDATE seo_runs SET status=?,payload=?,summary=? WHERE run_id=?",
                (run["status"], json.dumps(run), json.dumps(_run_summary(run)), run["run_id"]),
            )

    def run(self, project_id: str, run_id: str) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM seo_runs WHERE project_id=? AND run_id=?",
                (project_id, run_id),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def run_summaries(self, project_id: str) -> list[dict[str, Any]]:
        """Runs newest first, without rows, each with its call costs from one grouped query."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT summary FROM seo_runs WHERE project_id=? ORDER BY id DESC", (project_id,)
            ).fetchall()
            costs = conn.execute(
                "SELECT c.run_id, COUNT(*) AS calls, SUM(c.cost_usd) AS known,"
                " SUM(CASE WHEN c.cost_usd IS NULL THEN 1 ELSE 0 END) AS missing"
                " FROM seo_calls c JOIN seo_runs r ON r.run_id=c.run_id"
                " WHERE r.project_id=? GROUP BY c.run_id",
                (project_id,),
            ).fetchall()
        by_run = {row["run_id"]: dict(row) for row in costs}
        out = []
        for row in rows:
            summary = json.loads(row["summary"])
            summary["call_costs"] = by_run.get(summary["run_id"])
            out.append(summary)
        return out

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

    def start_serp(self, serp: dict[str, Any], key: str) -> bool:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO seo_serps(serp_id,project_id,idempotency_key,keyword,location_code,"
                "language_code,device,depth,created_at,status,item_count,cached,payload,items)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(project_id,idempotency_key) DO NOTHING",
                (
                    serp["serp_id"],
                    serp["project_id"],
                    key,
                    serp["keyword"],
                    serp["location_code"],
                    serp["language_code"],
                    serp["device"],
                    serp["depth"],
                    int(time.time()),
                    serp["status"],
                    len(serp["items"]),
                    int(serp["cached"]),
                    json.dumps({k: v for k, v in serp.items() if k != "items"}),
                    json.dumps(serp["items"]),
                ),
            )
            return bool(cur.rowcount == 1)

    def update_serp(self, serp: dict[str, Any]) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE seo_serps SET status=?,item_count=?,payload=?,items=? WHERE serp_id=?",
                (
                    serp["status"],
                    len(serp["items"]),
                    json.dumps({k: v for k, v in serp.items() if k != "items"}),
                    json.dumps(serp["items"]),
                    serp["serp_id"],
                ),
            )

    def serp_for_key(self, project_id: str, key: str) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT payload,items FROM seo_serps WHERE project_id=? AND idempotency_key=?",
                (project_id, key),
            ).fetchone()
        return {**json.loads(row["payload"]), "items": json.loads(row["items"])} if row else None

    def serp(self, project_id: str, serp_id: str) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT payload,items FROM seo_serps WHERE project_id=? AND serp_id=?",
                (project_id, serp_id),
            ).fetchone()
        return {**json.loads(row["payload"]), "items": json.loads(row["items"])} if row else None

    def cached_serp(
        self, project_id: str, market: dict[str, Any], depth: int, since: int
    ) -> dict[str, Any] | None:
        """Newest bought, non-empty snapshot at least ``depth`` deep observed after ``since``.

        Cache copies are skipped, so a copy never extends the original's freshness.
        """
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT payload,items FROM seo_serps WHERE project_id=? AND keyword=?"
                " AND location_code=? AND language_code=? AND device=? AND status='completed'"
                " AND cached=0 AND item_count>0 AND depth>=? AND created_at>?"
                " ORDER BY id DESC LIMIT 1",
                (
                    project_id,
                    market["keyword"],
                    market["location_code"],
                    market["language_code"],
                    market["device"],
                    depth,
                    since,
                ),
            ).fetchone()
        return {**json.loads(row["payload"]), "items": json.loads(row["items"])} if row else None

    def serps(self, project_id: str, keyword: str | None = None) -> list[dict[str, Any]]:
        """Snapshot summaries newest first, without their result rows."""
        sql = "SELECT payload,item_count FROM seo_serps WHERE project_id=?"
        args: list[Any] = [project_id]
        if keyword is not None:
            sql += " AND keyword=?"
            args.append(keyword)
        with self._db.connect() as conn:
            rows = conn.execute(sql + " ORDER BY id DESC", args).fetchall()
        return [json.loads(row["payload"]) | {"item_count": row["item_count"]} for row in rows]

    def start_lookup(self, lookup: dict[str, Any], key: str) -> bool:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO seo_lookups(lookup_id,project_id,idempotency_key,kind,target_key,"
                "location_code,language_code,created_at,status,item_count,cached,payload,items)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(project_id,idempotency_key) DO NOTHING",
                (
                    lookup["lookup_id"],
                    lookup["project_id"],
                    key,
                    lookup["kind"],
                    lookup["target_key"],
                    lookup["location_code"],
                    lookup["language_code"],
                    int(time.time()),
                    lookup["status"],
                    len(lookup["items"]),
                    int(lookup["cached"]),
                    json.dumps({k: v for k, v in lookup.items() if k != "items"}),
                    json.dumps(lookup["items"]),
                ),
            )
            return bool(cur.rowcount == 1)

    def update_lookup(self, lookup: dict[str, Any]) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE seo_lookups SET status=?,item_count=?,payload=?,items=? WHERE lookup_id=?",
                (
                    lookup["status"],
                    len(lookup["items"]),
                    json.dumps({k: v for k, v in lookup.items() if k != "items"}),
                    json.dumps(lookup["items"]),
                    lookup["lookup_id"],
                ),
            )

    def _lookup_where(self, where: str, args: tuple[Any, ...]) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT payload,items FROM seo_lookups WHERE " + where, args
            ).fetchone()
        return {**json.loads(row["payload"]), "items": json.loads(row["items"])} if row else None

    def lookup_for_key(self, project_id: str, key: str) -> dict[str, Any] | None:
        return self._lookup_where("project_id=? AND idempotency_key=?", (project_id, key))

    def lookup(self, project_id: str, lookup_id: str) -> dict[str, Any] | None:
        return self._lookup_where("project_id=? AND lookup_id=?", (project_id, lookup_id))

    def cached_lookup(self, lookup: dict[str, Any], since: int) -> dict[str, Any] | None:
        """Newest bought, non-empty lookup of the same target and market after ``since``."""
        return self._lookup_where(
            "project_id=? AND kind=? AND target_key=? AND location_code=? AND language_code=?"
            " AND status='completed' AND cached=0 AND item_count>0 AND created_at>?"
            " ORDER BY id DESC LIMIT 1",
            (
                lookup["project_id"],
                lookup["kind"],
                lookup["target_key"],
                lookup["location_code"],
                lookup["language_code"],
                since,
            ),
        )

    def lookups(self, project_id: str, kinds: Sequence[str]) -> list[dict[str, Any]]:
        """Summaries of the ``kinds`` lookups newest first, without their result rows."""
        marks = ",".join("?" * len(kinds))
        with self._db.connect() as conn:
            rows = conn.execute(
                f"SELECT payload,item_count FROM seo_lookups WHERE project_id=? AND kind IN ({marks})"
                " ORDER BY id DESC",
                (project_id, *kinds),
            ).fetchall()
        return [json.loads(row["payload"]) | {"item_count": row["item_count"]} for row in rows]

    def context(self, project_id: str, log_limit: int) -> dict[str, list[dict[str, Any]]]:
        with self._db.connect() as conn:
            sections = conn.execute(
                "SELECT section_key,title,content,updated_at,updated_by FROM seo_context_sections"
                " WHERE project_id=? ORDER BY section_key",
                (project_id,),
            ).fetchall()
            competitors = conn.execute(
                "SELECT domain,name,notes,updated_at,updated_by FROM seo_competitors"
                " WHERE project_id=? ORDER BY domain",
                (project_id,),
            ).fetchall()
            pages = conn.execute(
                "SELECT url,role,topic,notes,updated_at,updated_by FROM seo_key_pages"
                " WHERE project_id=? ORDER BY url",
                (project_id,),
            ).fetchall()
            log = conn.execute(
                "SELECT log_id,entry_date,summary,created_by,created_at FROM seo_research_log"
                " WHERE project_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
                (project_id, log_limit),
            ).fetchall()
        return {
            "sections": [dict(row) for row in sections],
            "competitors": [dict(row) for row in competitors],
            "key_pages": [dict(row) for row in pages],
            "research_log": [dict(row) for row in log],
        }

    def apply_context(
        self, project_id: str, ops: list[tuple[str, Any]], author: str, now: str
    ) -> None:
        """Apply resolved context writes in one transaction: all of them or none."""
        with self._db.transaction() as conn:
            conn.execute(
                "SELECT project_id FROM seo_projects WHERE project_id=?"
                + (" FOR UPDATE" if self._db.dialect == "postgresql" else ""),
                (project_id,),
            ).fetchone()
            for kind, value in ops:
                if kind == "upsert_section":
                    conn.execute(
                        "INSERT INTO seo_context_sections(project_id,section_key,title,content,"
                        "updated_at,updated_by) VALUES(?,?,?,?,?,?)"
                        " ON CONFLICT(project_id,section_key) DO UPDATE SET"
                        " title=coalesce(excluded.title,seo_context_sections.title),"
                        " content=excluded.content,updated_at=excluded.updated_at,"
                        " updated_by=excluded.updated_by",
                        (project_id, value["key"], value["title"], value["content"], now, author),
                    )
                elif kind == "delete_section":
                    conn.execute(
                        "DELETE FROM seo_context_sections WHERE project_id=? AND section_key=?",
                        (project_id, value),
                    )
                elif kind == "upsert_competitor":
                    conn.execute(
                        "INSERT INTO seo_competitors(project_id,domain,name,notes,updated_at,"
                        "updated_by) VALUES(?,?,?,?,?,?)"
                        " ON CONFLICT(project_id,domain) DO UPDATE SET"
                        " name=coalesce(excluded.name,seo_competitors.name),"
                        " notes=coalesce(excluded.notes,seo_competitors.notes),"
                        " updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                        (project_id, value["domain"], value["name"], value["notes"], now, author),
                    )
                elif kind == "delete_competitor":
                    conn.execute(
                        "DELETE FROM seo_competitors WHERE project_id=? AND domain=?",
                        (project_id, value),
                    )
                elif kind == "upsert_key_page":
                    # An omitted role keeps the stored one; a new row defaults to "other".
                    conn.execute(
                        "INSERT INTO seo_key_pages(project_id,url,role,topic,notes,updated_at,"
                        "updated_by) VALUES(?,?,?,?,?,?,?)"
                        " ON CONFLICT(project_id,url) DO UPDATE SET"
                        + (" role=excluded.role," if value["role"] else "")
                        + " topic=coalesce(excluded.topic,seo_key_pages.topic),"
                        " notes=coalesce(excluded.notes,seo_key_pages.notes),"
                        " updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                        (
                            project_id,
                            value["url"],
                            value["role"] or "other",
                            value["topic"],
                            value["notes"],
                            now,
                            author,
                        ),
                    )
                elif kind == "delete_key_page":
                    conn.execute(
                        "DELETE FROM seo_key_pages WHERE project_id=? AND url=?",
                        (project_id, value),
                    )
                elif kind == "append_log":
                    conn.execute(
                        "INSERT INTO seo_research_log(log_id,project_id,entry_date,summary,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?)",
                        (new_ulid(), project_id, now[:10], value["summary"], author, now),
                    )
                    # The log only answers "was this already studied recently?".
                    conn.execute(
                        "DELETE FROM seo_research_log WHERE project_id=? AND entry_date<?",
                        (project_id, value["prune_before"]),
                    )
                elif kind == "delete_log":
                    conn.execute(
                        "DELETE FROM seo_research_log WHERE project_id=? AND log_id=?",
                        (project_id, value),
                    )
                else:
                    raise ValueError(kind)

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
            if (
                "context_id" not in context
                and [json.loads(row["payload"]) for row in current_keywords]
                != context["saved_keywords"]
            ):
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

    def save_context(self, value: dict[str, Any]) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO seo_report_contexts(context_id,project_id,payload) VALUES(?,?,?)",
                (value["context_id"], value["project_id"], json.dumps(value)),
            )

    def report_bundle(self, project_id: str, context_id: str) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM seo_report_contexts WHERE project_id=? AND context_id=?",
                (project_id, context_id),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def actions(self, project_id: str) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM seo_actions WHERE project_id=? ORDER BY id DESC", (project_id,)
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def create_action(self, value: dict[str, Any], source_key: str) -> dict[str, Any]:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO seo_actions(action_id,project_id,source_key,payload) VALUES(?,?,?,?) "
                "ON CONFLICT(project_id,source_key) DO NOTHING",
                (value["action_id"], value["project_id"], source_key, json.dumps(value)),
            )
            row = conn.execute(
                "SELECT payload FROM seo_actions WHERE project_id=? AND source_key=?",
                (value["project_id"], source_key),
            ).fetchone()
        assert row is not None
        return dict(json.loads(row["payload"]))

    def update_action(self, value: dict[str, Any], version: int) -> bool:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE seo_actions SET payload=?,version=? WHERE project_id=? "
                "AND action_id=? AND version=?",
                (
                    json.dumps(value),
                    value["version"],
                    value["project_id"],
                    value["action_id"],
                    version,
                ),
            )
        return bool(cur.rowcount == 1)
