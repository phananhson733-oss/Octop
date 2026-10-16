-- Fork-only SEO tables. Not a numbered migration: run_migrations() applies this file
-- idempotently on every start through _ensure_seo_schema(), see migrate.py.
CREATE TABLE IF NOT EXISTS seo_projects (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id TEXT NOT NULL UNIQUE,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 -- Deleting the bound agent keeps the project and its paid evidence, which can be rebound.
 agent_id TEXT UNIQUE REFERENCES agents(agent_id) ON DELETE SET NULL,
 name TEXT NOT NULL, domain TEXT NOT NULL,
 location_code INTEGER NOT NULL, language_code TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seo_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 run_id TEXT NOT NULL UNIQUE,
 project_id TEXT NOT NULL REFERENCES seo_projects(project_id) ON DELETE CASCADE,
 idempotency_key TEXT NOT NULL, input_hash TEXT NOT NULL,
 created_at INTEGER NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
 -- payload without keyword rows, for listing runs.
 summary TEXT NOT NULL DEFAULT '{}',
 UNIQUE(project_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS seo_runs_cache ON seo_runs(project_id,input_hash,created_at);
CREATE TABLE IF NOT EXISTS seo_calls (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 call_id TEXT NOT NULL UNIQUE,
 run_id TEXT NOT NULL REFERENCES seo_runs(run_id) ON DELETE CASCADE,
 source TEXT NOT NULL, seed TEXT NOT NULL,
 status TEXT NOT NULL, cost_usd DOUBLE PRECISION, error TEXT, receipt_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seo_keywords (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 keyword_id TEXT NOT NULL UNIQUE,
 project_id TEXT NOT NULL REFERENCES seo_projects(project_id) ON DELETE CASCADE,
 keyword TEXT NOT NULL, location_code INTEGER NOT NULL, language_code TEXT NOT NULL,
 payload TEXT NOT NULL,
 UNIQUE(project_id, keyword, location_code, language_code)
);
CREATE TABLE IF NOT EXISTS seo_tags (
 project_id TEXT NOT NULL REFERENCES seo_projects(project_id) ON DELETE CASCADE,
 normalized_name TEXT NOT NULL, name TEXT NOT NULL,
 PRIMARY KEY(project_id,normalized_name)
);
CREATE TABLE IF NOT EXISTS seo_reports (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 report_id TEXT NOT NULL UNIQUE,
 project_id TEXT NOT NULL REFERENCES seo_projects(project_id) ON DELETE CASCADE,
 run_id TEXT NOT NULL REFERENCES seo_runs(run_id) ON DELETE CASCADE,
 metadata TEXT NOT NULL, html TEXT NOT NULL, context_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seo_serps (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 serp_id TEXT NOT NULL UNIQUE,
 project_id TEXT NOT NULL REFERENCES seo_projects(project_id) ON DELETE CASCADE,
 idempotency_key TEXT NOT NULL, keyword TEXT NOT NULL,
 location_code INTEGER NOT NULL, language_code TEXT NOT NULL, device TEXT NOT NULL,
 depth INTEGER NOT NULL, created_at INTEGER NOT NULL, status TEXT NOT NULL,
 item_count INTEGER NOT NULL DEFAULT 0, cached INTEGER NOT NULL DEFAULT 0,
 -- payload holds the snapshot without its result rows, which live in items.
 payload TEXT NOT NULL, items TEXT NOT NULL DEFAULT '[]',
 UNIQUE(project_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS seo_serps_lookup
 ON seo_serps(project_id,keyword,location_code,language_code,device,created_at);
CREATE TABLE IF NOT EXISTS seo_context_sections (
 project_id TEXT NOT NULL REFERENCES seo_projects(project_id) ON DELETE CASCADE,
 section_key TEXT NOT NULL, title TEXT, content TEXT NOT NULL,
 updated_at TEXT NOT NULL, updated_by TEXT NOT NULL,
 PRIMARY KEY(project_id,section_key)
);
CREATE TABLE IF NOT EXISTS seo_competitors (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id TEXT NOT NULL REFERENCES seo_projects(project_id) ON DELETE CASCADE,
 domain TEXT NOT NULL, name TEXT, notes TEXT,
 updated_at TEXT NOT NULL, updated_by TEXT NOT NULL,
 UNIQUE(project_id,domain)
);
CREATE TABLE IF NOT EXISTS seo_key_pages (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id TEXT NOT NULL REFERENCES seo_projects(project_id) ON DELETE CASCADE,
 url TEXT NOT NULL, role TEXT NOT NULL, topic TEXT, notes TEXT,
 updated_at TEXT NOT NULL, updated_by TEXT NOT NULL,
 UNIQUE(project_id,url)
);
CREATE TABLE IF NOT EXISTS seo_research_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 log_id TEXT NOT NULL UNIQUE,
 project_id TEXT NOT NULL REFERENCES seo_projects(project_id) ON DELETE CASCADE,
 entry_date TEXT NOT NULL, summary TEXT NOT NULL,
 created_by TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS seo_research_log_project ON seo_research_log(project_id,created_at);
-- Site audits. The crawl frontier, pages, links and issues are written as the crawl
-- runs, so an audit interrupted by a restart resumes where it stopped.
CREATE TABLE IF NOT EXISTS seo_audits (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 audit_id TEXT NOT NULL UNIQUE,
 project_id TEXT NOT NULL REFERENCES seo_projects(project_id) ON DELETE CASCADE,
 started_by INTEGER NOT NULL, idempotency_key TEXT NOT NULL,
 requested_url TEXT NOT NULL, start_url TEXT, max_pages INTEGER NOT NULL,
 -- status: queued|running|completed|failed|cancelled; phase: discovery|crawl|render|finalize.
 status TEXT NOT NULL, phase TEXT NOT NULL,
 pages_crawled INTEGER NOT NULL DEFAULT 0, pages_total INTEGER NOT NULL DEFAULT 0,
 discovered_count INTEGER NOT NULL DEFAULT 0, sitemap_count INTEGER NOT NULL DEFAULT 0,
 -- robots.txt as fetched (NULL when missing), so a resumed crawl keeps the same rules.
 robots_text TEXT,
 crawl_complete INTEGER, rate_limited INTEGER NOT NULL DEFAULT 0,
 error_code TEXT, error_detail TEXT, failed_phase TEXT,
 cancel_requested INTEGER NOT NULL DEFAULT 0,
 -- The claiming process. Every crawl write checks it, so two processes never interleave.
 runner_token TEXT, heartbeat_at INTEGER,
 created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
 UNIQUE(project_id,idempotency_key)
);
-- At most one queued or running audit per project.
CREATE UNIQUE INDEX IF NOT EXISTS seo_audits_active
 ON seo_audits(project_id) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS seo_audit_frontier (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 audit_id TEXT NOT NULL REFERENCES seo_audits(audit_id) ON DELETE CASCADE,
 url TEXT NOT NULL, depth INTEGER, source TEXT NOT NULL,
 in_sitemap INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending',
 -- Path shape (/products/*/*), so the crawl budget is spread across page templates.
 cluster TEXT,
 UNIQUE(audit_id,url)
);
CREATE INDEX IF NOT EXISTS seo_audit_frontier_next ON seo_audit_frontier(audit_id,state);
CREATE TABLE IF NOT EXISTS seo_audit_pages (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 audit_id TEXT NOT NULL REFERENCES seo_audits(audit_id) ON DELETE CASCADE,
 url TEXT NOT NULL, status_code INTEGER NOT NULL, fetch_class TEXT NOT NULL, redirect_url TEXT,
 title TEXT NOT NULL DEFAULT '', meta_description TEXT NOT NULL DEFAULT '',
 canonical_url TEXT, header_canonical_url TEXT, robots_meta TEXT, x_robots_tag TEXT,
 og_title TEXT, og_description TEXT, og_image TEXT,
 h1_count INTEGER NOT NULL DEFAULT 0, heading_order TEXT NOT NULL DEFAULT '[]',
 word_count INTEGER NOT NULL DEFAULT 0, content_hash TEXT,
 is_html INTEGER NOT NULL DEFAULT 0, javascript_shell INTEGER NOT NULL DEFAULT 0,
 images_total INTEGER NOT NULL DEFAULT 0, images_missing_alt INTEGER NOT NULL DEFAULT 0,
 internal_link_count INTEGER NOT NULL DEFAULT 0, external_link_count INTEGER NOT NULL DEFAULT 0,
 has_structured_data INTEGER NOT NULL DEFAULT 0, hreflang TEXT NOT NULL DEFAULT '[]',
 is_indexable INTEGER NOT NULL DEFAULT 0, response_time_ms INTEGER NOT NULL DEFAULT 0,
 crawl_depth INTEGER, in_sitemap INTEGER NOT NULL DEFAULT 0, crawled_at TEXT NOT NULL,
 -- Internal link targets as a JSON array (at most 500), for broken-link and orphan checks.
 internal_links TEXT NOT NULL DEFAULT '[]',
 -- H1 texts and [hreflang, href] alternates as JSON arrays, for cross-page checks.
 h1s TEXT NOT NULL DEFAULT '[]', alternates TEXT NOT NULL DEFAULT '[]',
 -- The HTML was cut at the size limit, so links and markup past the cut are unseen.
 body_truncated INTEGER NOT NULL DEFAULT 0,
 -- What headless Chromium saw on a sampled page (JSON), compared with the HTML at finalize.
 render TEXT,
 UNIQUE(audit_id,url)
);
CREATE TABLE IF NOT EXISTS seo_audit_issues (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 audit_id TEXT NOT NULL REFERENCES seo_audits(audit_id) ON DELETE CASCADE,
 issue_type TEXT NOT NULL, severity TEXT NOT NULL, page_url TEXT NOT NULL, details TEXT
);
CREATE INDEX IF NOT EXISTS seo_audit_issues_type ON seo_audit_issues(audit_id,issue_type);
