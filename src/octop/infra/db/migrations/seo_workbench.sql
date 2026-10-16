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
