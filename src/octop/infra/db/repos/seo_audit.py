"""SQL persistence for site audits.

Crawl writes are fenced: each one first updates the audit row only if the caller
still holds its claim token and nobody asked to cancel. When that update misses,
the writer stops and nothing else in its transaction is applied.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from octop.infra.db.pool import DatabasePool

_ROW = (
    "audit_id,project_id,started_by,requested_url,start_url,max_pages,status,phase,"
    "pages_crawled,pages_total,discovered_count,sitemap_count,crawl_complete,rate_limited,"
    "error_code,error_detail,failed_phase,cancel_requested,created_at,started_at,completed_at"
)
_PAGE = (
    "url,status_code,fetch_class,redirect_url,title,meta_description,canonical_url,"
    "header_canonical_url,robots_meta,x_robots_tag,og_title,og_description,og_image,h1_count,"
    "heading_order,word_count,content_hash,is_html,javascript_shell,images_total,"
    "images_missing_alt,internal_link_count,external_link_count,has_structured_data,hreflang,"
    "is_indexable,response_time_ms,crawl_depth,in_sitemap,crawled_at,h1s,alternates"
)
# Page columns stored as JSON text.
_JSON_COLUMNS = frozenset({"heading_order", "hreflang", "internal_links", "h1s", "alternates"})
_PAGE_COLUMNS = _PAGE.split(",")
_BOOLEANS = (
    "is_html",
    "javascript_shell",
    "has_structured_data",
    "is_indexable",
    "in_sitemap",
    "cancel_requested",
    "rate_limited",
)
_CHUNK = 200
_FENCE = (
    "UPDATE seo_audits SET heartbeat_at=? WHERE audit_id=? AND runner_token=? "
    "AND status='running' AND cancel_requested=0"
)


def _audit(row: Any) -> dict[str, Any]:
    out = dict(row)
    for key in ("cancel_requested", "rate_limited"):
        out[key] = bool(out[key])
    if out.get("crawl_complete") is not None:
        out["crawl_complete"] = bool(out["crawl_complete"])
    return out


def _page(row: Any) -> dict[str, Any]:
    out = dict(row)
    for key in _BOOLEANS:
        if key in out:
            out[key] = bool(out[key])
    for key in _JSON_COLUMNS:
        if key in out:
            out[key] = json.loads(out[key])
    return out


def _chunks(items: list[Any]) -> Iterable[list[Any]]:
    for start in range(0, len(items), _CHUNK):
        yield items[start : start + _CHUNK]


class SeoAuditRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def _lock(self) -> str:
        return " FOR UPDATE" if self._db.dialect == "postgresql" else ""

    # Lifecycle -------------------------------------------------------------------

    def create(self, audit: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        """Insert a queued audit: ``created``, ``replay`` (same key) or ``active`` (busy)."""
        with self._db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO seo_audits(audit_id,project_id,started_by,idempotency_key,"
                "requested_url,max_pages,status,phase,created_at) "
                "VALUES(?,?,?,?,?,?,'queued','discovery',?) ON CONFLICT DO NOTHING",
                (
                    audit["audit_id"],
                    audit["project_id"],
                    audit["started_by"],
                    audit["idempotency_key"],
                    audit["requested_url"],
                    audit["max_pages"],
                    audit["created_at"],
                ),
            )
            created = cur.rowcount == 1
            key = "audit_id=?" if created else "project_id=? AND idempotency_key=?"
            args = (
                (audit["audit_id"],) if created else (audit["project_id"], audit["idempotency_key"])
            )
            row = conn.execute(
                f"SELECT {_ROW},idempotency_key FROM seo_audits WHERE {key}", args
            ).fetchone()
        if created:
            return "created", _audit(row)
        return ("replay", _audit(row)) if row is not None else ("active", None)

    def get(self, project_id: str, audit_id: str) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                f"SELECT {_ROW},idempotency_key FROM seo_audits WHERE project_id=? AND audit_id=?",
                (project_id, audit_id),
            ).fetchone()
        return _audit(row) if row else None

    def latest(self, project_id: str) -> dict[str, Any] | None:
        audits = self.list_audits(project_id, limit=1)
        return audits[0] if audits else None

    def list_audits(self, project_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute(
                f"SELECT {_ROW},idempotency_key FROM seo_audits WHERE project_id=? "
                "ORDER BY id DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [_audit(row) for row in rows]

    def claimable(self, stale_before: int) -> list[str]:
        """Unfinished audits nobody holds: queued, or running with a stale heartbeat."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT audit_id FROM seo_audits WHERE status IN ('queued','running') "
                "AND (runner_token IS NULL OR heartbeat_at IS NULL OR heartbeat_at<?) ORDER BY id",
                (stale_before,),
            ).fetchall()
        return [str(row["audit_id"]) for row in rows]

    def claim(
        self, audit_id: str, token: str, now: int, stale_before: int, started_at: str
    ) -> dict[str, Any] | None:
        """Take the audit for ``token``; an audit cancelled while unheld ends as cancelled."""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT status,cancel_requested,runner_token,heartbeat_at FROM seo_audits "
                "WHERE audit_id=?" + self._lock(),
                (audit_id,),
            ).fetchone()
            if row is None or row["status"] not in {"queued", "running"}:
                return None
            held = (
                row["runner_token"] not in {None, token}
                and row["heartbeat_at"] is not None
                and row["heartbeat_at"] >= stale_before
            )
            if held:
                return None
            if row["cancel_requested"]:
                conn.execute(
                    "UPDATE seo_audits SET status='cancelled',completed_at=? WHERE audit_id=?",
                    (started_at, audit_id),
                )
                return None
            conn.execute(
                "UPDATE seo_audits SET status='running',runner_token=?,heartbeat_at=?,"
                "started_at=COALESCE(started_at,?) WHERE audit_id=?",
                (token, now, started_at, audit_id),
            )
            claimed = conn.execute(
                f"SELECT {_ROW},robots_text FROM seo_audits WHERE audit_id=?", (audit_id,)
            ).fetchone()
        return _audit(claimed)

    def beat(self, audit_id: str, token: str, now: int) -> bool:
        with self._db.transaction() as conn:
            return bool(conn.execute(_FENCE, (now, audit_id, token)).rowcount == 1)

    def release(self, audit_id: str, token: str) -> None:
        """Give up a claim at shutdown; the row stays running for the next process."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE seo_audits SET runner_token=NULL WHERE audit_id=? AND runner_token=? "
                "AND status='running'",
                (audit_id, token),
            )

    def request_cancel(self, project_id: str, audit_id: str, now: str) -> dict[str, Any] | None:
        """Ask a running audit to stop; a queued one nobody holds is cancelled at once."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE seo_audits SET cancel_requested=1 WHERE project_id=? AND audit_id=? "
                "AND status IN ('queued','running')",
                (project_id, audit_id),
            )
            conn.execute(
                "UPDATE seo_audits SET status='cancelled',completed_at=? WHERE project_id=? "
                "AND audit_id=? AND status='queued' AND runner_token IS NULL",
                (now, project_id, audit_id),
            )
        return self.get(project_id, audit_id)

    def mark_cancelled(self, audit_id: str, token: str, now: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE seo_audits SET status='cancelled',completed_at=? WHERE audit_id=? "
                "AND runner_token=? AND status='running' AND cancel_requested=1",
                (now, audit_id, token),
            )

    def fail(self, audit_id: str, token: str, code: str, detail: str, phase: str, now: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE seo_audits SET status='failed',error_code=?,error_detail=?,"
                "failed_phase=?,completed_at=? WHERE audit_id=? AND runner_token=? "
                "AND status='running'",
                (code, detail[:500], phase, now, audit_id, token),
            )

    def delete(self, project_id: str, audit_id: str) -> bool:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM seo_audits WHERE project_id=? AND audit_id=?", (project_id, audit_id)
            )
            return bool(cur.rowcount == 1)

    # Crawl -----------------------------------------------------------------------

    def save_discovery(
        self,
        audit_id: str,
        token: str,
        now: int,
        *,
        start_url: str,
        robots_text: str | None,
        start: tuple[str, str] | None,
        seeds: list[tuple[str, str]],
        issues: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Seed the frontier and move to the crawl phase; repeating it changes nothing.

        ``start`` and ``seeds`` are ``(url, cluster)``; ``issues`` are the site-level
        findings discovery made (sitemap URLs robots.txt blocks).
        """
        with self._db.transaction() as conn:
            if conn.execute(_FENCE, (now, audit_id, token)).rowcount != 1:
                return False
            if start is not None:
                conn.execute(
                    "INSERT INTO seo_audit_frontier(audit_id,url,depth,source,in_sitemap,cluster) "
                    "VALUES(?,?,0,'link',0,?) ON CONFLICT(audit_id,url) DO NOTHING",
                    (audit_id, *start),
                )
            for chunk in _chunks(seeds):
                # A URL already queued (the start URL) keeps its place and gains the flag.
                conn.execute(
                    "INSERT INTO seo_audit_frontier(audit_id,url,depth,source,in_sitemap,cluster) "
                    "VALUES "
                    + ",".join(["(?,?,NULL,'sitemap',1,?)"] * len(chunk))
                    + " ON CONFLICT(audit_id,url) DO UPDATE SET in_sitemap=1",
                    tuple(value for seed in chunk for value in (audit_id, *seed)),
                )
            self._insert_issues(conn, audit_id, issues or [])
            conn.execute(
                "UPDATE seo_audits SET start_url=?,robots_text=?,sitemap_count=?,phase='crawl' "
                "WHERE audit_id=?",
                (start_url, robots_text, len(seeds), audit_id),
            )
            self._progress(conn, audit_id)
        return True

    def _progress(self, conn: Any, audit_id: str) -> None:
        counts = conn.execute(
            "SELECT COUNT(*) AS seen, "
            "COALESCE(SUM(CASE WHEN state='done' THEN 1 ELSE 0 END),0) AS done "
            "FROM seo_audit_frontier WHERE audit_id=?",
            (audit_id,),
        ).fetchone()
        conn.execute(
            "UPDATE seo_audits SET discovered_count=?,pages_crawled=?,"
            "pages_total=CASE WHEN ?<max_pages THEN ? ELSE max_pages END WHERE audit_id=?",
            (counts["seen"], counts["done"], counts["seen"], counts["seen"], audit_id),
        )

    def next_url(self, audit_id: str) -> dict[str, Any] | None:
        """The next URL, taking the page templates in turns, shallowest first within a turn.

        A URL from the cluster with the fewest crawled pages goes first, so a 50-page
        budget sees every section before a second product page. Within that, link-found
        before sitemap-only, then by depth: a sitemap seed reached by a link keeps its
        seeding id, so ``id`` alone would replay the sitemap's order.
        """
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT f.url,f.depth,f.in_sitemap FROM seo_audit_frontier f LEFT JOIN "
                "(SELECT cluster,COUNT(*) AS n FROM seo_audit_frontier WHERE audit_id=? "
                "AND state='done' GROUP BY cluster) d ON d.cluster=f.cluster "
                "WHERE f.audit_id=? AND f.state='pending' ORDER BY COALESCE(d.n,0), "
                "CASE f.source WHEN 'link' THEN 0 ELSE 1 END, f.depth IS NULL, f.depth, f.id "
                "LIMIT 1",
                (audit_id, audit_id),
            ).fetchone()
        return {**dict(row), "in_sitemap": bool(row["in_sitemap"])} if row else None

    def pending_count(self, audit_id: str) -> int:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM seo_audit_frontier WHERE audit_id=? AND state='pending'",
                (audit_id,),
            ).fetchone()
        return int(row["n"])

    def record_page(
        self,
        audit_id: str,
        token: str,
        now: int,
        page: dict[str, Any],
        issues: list[dict[str, Any]],
        discovered: list[tuple[str, int | None, str]],
    ) -> bool:
        """Store one crawled page with its issues and the URLs it adds, all or nothing.

        ``discovered`` holds ``(url, depth, cluster)``.
        """
        with self._db.transaction() as conn:
            if conn.execute(_FENCE, (now, audit_id, token)).rowcount != 1:
                return False
            values = [
                json.dumps(page[key])
                if key in _JSON_COLUMNS
                else int(page[key])
                if key in _BOOLEANS
                else page[key]
                for key in [*_PAGE_COLUMNS, "internal_links"]
            ]
            conn.execute(
                f"INSERT INTO seo_audit_pages(audit_id,{_PAGE},internal_links) "
                f"VALUES(?,{','.join(['?'] * len(values))}) ON CONFLICT(audit_id,url) DO NOTHING",
                (audit_id, *values),
            )
            self._insert_issues(conn, audit_id, issues)
            for chunk in _chunks(discovered):
                # A pending sitemap seed reached by a link takes the link's depth and queue.
                conn.execute(
                    "INSERT INTO seo_audit_frontier(audit_id,url,depth,source,in_sitemap,cluster) "
                    "VALUES "
                    + ",".join(["(?,?,?,'link',0,?)"] * len(chunk))
                    + " ON CONFLICT(audit_id,url) DO UPDATE SET source='link',depth=excluded.depth "
                    "WHERE seo_audit_frontier.state='pending' AND seo_audit_frontier.depth IS NULL "
                    "AND excluded.depth IS NOT NULL",
                    tuple(value for entry in chunk for value in (audit_id, *entry)),
                )
            conn.execute(
                "UPDATE seo_audit_frontier SET state='done' WHERE audit_id=? AND url=?",
                (audit_id, page["url"]),
            )
            self._progress(conn, audit_id)
        return True

    def _insert_issues(self, conn: Any, audit_id: str, issues: list[dict[str, Any]]) -> None:
        for chunk in _chunks(issues):
            conn.execute(
                "INSERT INTO seo_audit_issues(audit_id,issue_type,severity,page_url,details) "
                "VALUES " + ",".join(["(?,?,?,?,?)"] * len(chunk)),
                tuple(
                    value
                    for issue in chunk
                    for value in (
                        audit_id,
                        issue["issue_type"],
                        issue["severity"],
                        issue["page_url"],
                        json.dumps(issue["details"]) if issue["details"] is not None else None,
                    )
                ),
            )

    def start_finalize(
        self, audit_id: str, token: str, now: int, *, crawl_complete: bool, rate_limited: bool
    ) -> bool:
        with self._db.transaction() as conn:
            if conn.execute(_FENCE, (now, audit_id, token)).rowcount != 1:
                return False
            conn.execute(
                "UPDATE seo_audits SET phase='finalize',crawl_complete=?,rate_limited=? "
                "WHERE audit_id=?",
                (int(crawl_complete), int(rate_limited), audit_id),
            )
        return True

    def complete(
        self,
        audit_id: str,
        token: str,
        now: int,
        finished_at: str,
        issues: list[dict[str, Any]],
        replace_types: Iterable[str],
    ) -> bool:
        """Replace the cross-page issues and finish; a retried finalize writes them once."""
        types = sorted(replace_types)
        with self._db.transaction() as conn:
            if conn.execute(_FENCE, (now, audit_id, token)).rowcount != 1:
                return False
            conn.execute(
                "DELETE FROM seo_audit_issues WHERE audit_id=? AND issue_type IN ("
                + ",".join(["?"] * len(types))
                + ")",
                (audit_id, *types),
            )
            self._insert_issues(conn, audit_id, issues)
            conn.execute(
                "UPDATE seo_audits SET status='completed',completed_at=? WHERE audit_id=?",
                (finished_at, audit_id),
            )
        return True

    # Reads -------------------------------------------------------------------------

    def pages(self, audit_id: str, *, with_links: bool = False) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute(
                f"SELECT {_PAGE}"
                + (",internal_links" if with_links else "")
                + " FROM seo_audit_pages WHERE audit_id=? ORDER BY id",
                (audit_id,),
            ).fetchall()
        return [_page(row) for row in rows]

    def issues(self, audit_id: str) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT issue_type,severity,page_url,details FROM seo_audit_issues "
                "WHERE audit_id=? ORDER BY id",
                (audit_id,),
            ).fetchall()
        return [
            {**dict(row), "details": json.loads(row["details"]) if row["details"] else None}
            for row in rows
        ]
