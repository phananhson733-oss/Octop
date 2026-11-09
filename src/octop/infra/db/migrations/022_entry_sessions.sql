-- Ephemeral unified-entry credentials; no changes to existing user/business data.
CREATE TABLE IF NOT EXISTS entry_sessions (
    session_key TEXT PRIMARY KEY,
    payload TEXT,
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'pending', 'done'))
);
CREATE INDEX IF NOT EXISTS idx_entry_sessions_state_key ON entry_sessions(state, session_key);
-- Unified-entry admin operations: desired grants per bound account and each delivery's outcome.
CREATE TABLE IF NOT EXISTS entry_admin_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL UNIQUE,
    account TEXT NOT NULL,
    instance TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    desired TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'applied', 'partial', 'rejected')),
    result TEXT,
    readback TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (instance, user_id, version)
);
CREATE INDEX IF NOT EXISTS idx_entry_admin_operations_state ON entry_admin_operations(state, id);
CREATE INDEX IF NOT EXISTS idx_entry_admin_operations_account ON entry_admin_operations(account, id);
UPDATE _schema_version SET version = 22;
