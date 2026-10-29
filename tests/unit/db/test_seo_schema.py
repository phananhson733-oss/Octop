"""Fork-only SEO tables stay outside the numbered migration sequence."""

from __future__ import annotations

from pathlib import Path

from octop.infra.db.migrate import _max_discovered_version, run_migrations
from octop.infra.db.pool import SqlitePool

SEO_TABLES = {
    "seo_projects",
    "seo_runs",
    "seo_calls",
    "seo_keywords",
    "seo_tags",
    "seo_reports",
    "seo_serps",
    "seo_context_sections",
    "seo_competitors",
    "seo_key_pages",
    "seo_research_log",
    "seo_audits",
    "seo_audit_frontier",
    "seo_audit_pages",
    "seo_audit_issues",
    "seo_google_states",
    "seo_google_grants",
    "seo_gsc_connections",
    "seo_ga4_connections",
    "seo_lookups",
    "seo_rank_trackers",
    "seo_rank_keywords",
    "seo_rank_runs",
    "seo_rank_snapshots",
    "seo_ai_settings",
    "seo_ai_prompts",
    "seo_ai_runs",
    "seo_ai_answers",
}


def _state(pool: SqlitePool) -> tuple[int, set[str]]:
    with pool.connect() as conn:
        version = conn.execute("SELECT version FROM _schema_version").fetchone()[0]
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    return version, tables


def test_seo_tables_do_not_move_the_schema_watermark(tmp_path: Path) -> None:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    run_migrations(pool)
    version, tables = _state(pool)
    assert version == _max_discovered_version("sqlite")
    assert tables >= SEO_TABLES


def test_db_marked_by_the_earlier_numbered_seo_migration_returns_to_the_shared_max(
    tmp_path: Path,
) -> None:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    max_version = _max_discovered_version("sqlite")
    with pool.connect() as conn:
        conn.execute("UPDATE _schema_version SET version = ?", (max_version + 1,))
    run_migrations(pool)
    version, tables = _state(pool)
    # An upstream migration numbered max+1 would now run instead of being skipped.
    assert version == max_version
    assert tables >= SEO_TABLES


def test_existing_keyword_reports_survive_nullable_upgrade(tmp_path: Path) -> None:
    pool = SqlitePool(tmp_path / "upgrade.db")
    run_migrations(pool)
    with pool.transaction() as conn:
        conn.execute("DROP TABLE seo_reports")
        conn.execute(
            "CREATE TABLE seo_reports(id INTEGER PRIMARY KEY AUTOINCREMENT,report_id TEXT NOT NULL UNIQUE,"
            "project_id TEXT NOT NULL,run_id TEXT NOT NULL,metadata TEXT NOT NULL,html TEXT NOT NULL,context_json TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO users(username,role,created_at) VALUES('upgrade','admin',0)")
        uid = conn.execute("SELECT id FROM users WHERE username='upgrade'").fetchone()[0]
        conn.execute(
            "INSERT INTO seo_projects(project_id,user_id,name,domain,location_code,language_code,created_at) "
            "VALUES('p',?,'Site','example.com',2840,'en','2026-01-01')",
            (uid,),
        )
        conn.execute(
            "INSERT INTO seo_runs(run_id,project_id,idempotency_key,input_hash,created_at,status,payload) "
            "VALUES('r','p','key','hash',0,'completed','{}')"
        )
        conn.execute(
            "INSERT INTO seo_reports VALUES(7,'report','p','r','{}','<html>original</html>','{}')"
        )
    run_migrations(pool)
    run_migrations(pool)
    with pool.connect() as conn:
        row = conn.execute("SELECT * FROM seo_reports").fetchone()
        assert (row["id"], row["report_id"], row["html"]) == (7, "report", "<html>original</html>")
        cols = conn.execute("PRAGMA table_info(seo_reports)").fetchall()
        assert next(c for c in cols if c["name"] == "run_id")["notnull"] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    pool.close()
