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
