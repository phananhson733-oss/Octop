"""Explicit local-only candidate check. Reads a 0600 credential file, never prints it."""

from __future__ import annotations

import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import PostgresPool
from octop.infra.db.services import build_shared_services
from octop.infra.unified_entry.store import Binding, EntryStore
from octop.infra.utils.paths import PathLayout


def main() -> None:
    path = Path(os.environ["OCTOP_ENTRY_TEST_CREDENTIAL_FILE"])
    dsn = json.loads(path.read_text())["dsn"]
    address = urlsplit(dsn)
    if address.hostname != "127.0.0.1" or address.port != 55439 or address.path != "/entry_test":
        raise RuntimeError("Refusing non-candidate database")
    pools = [PostgresPool(dsn, max_size=4) for _ in range(2)]
    try:
        run_migrations(pools[0])
        # Candidate-only watermark repair; all rows here are synthetic fixtures.
        with pools[0].transaction() as conn:
            conn.execute(
                "INSERT INTO settings(key,value) VALUES ('entry-test-preserve','kept') ON CONFLICT(key) DO NOTHING"
            )
            conn.execute("DROP TABLE entry_sessions")
        run_migrations(pools[0])
        with pools[0].connect() as conn:
            assert (
                conn.execute(
                    "SELECT value FROM settings WHERE key='entry-test-preserve'"
                ).fetchone()[0]
                == "kept"
            )

        with tempfile.TemporaryDirectory(prefix="entry-pg-") as tmp:
            paths = PathLayout(Path(tmp))
            services = [
                build_shared_services(db=db, paths=paths, config=OctopConfig()) for db in pools
            ]
            key = Fernet.generate_key()
            stores = [EntryStore(service, key) for service in services]
            with ThreadPoolExecutor(max_workers=2) as executor:
                jobs = [
                    executor.submit(
                        store.register,
                        "customer/test",
                        Binding("global-test", "customer", "test", 7),
                    )
                    for store in stores
                ]
                for job in jobs:
                    job.result()
                for _ in range(30):
                    token = stores[0].create("customer/test", "synthetic-backend-token", 3600)
                    stale = stores[0].read(token)
                    assert stale is not None
                    a = executor.submit(stores[0]._save, token, stale)
                    b = executor.submit(stores[1].begin_revoke, token)
                    a.result()
                    b.result()
                    assert stores[0].read(token) is None
                    stores[1].finish_revoke(token)
                    stores[0]._save(token, stale)
                    assert stores[0].read(token, pending=True) is None
            with pools[0].transaction() as conn:
                conn.execute(
                    "INSERT INTO entry_sessions(session_key,state) SELECT 'history-' || n::text,'done' FROM generate_series(1,20000) n ON CONFLICT DO NOTHING"
                )
            token = stores[0].create("customer/test", "pending-backend", 3600)
            stores[0].begin_revoke(token)
            with pools[0].connect() as conn:
                conn.execute("ANALYZE entry_sessions")
                rows = conn.execute(
                    "EXPLAIN (ANALYZE, FORMAT JSON) SELECT session_key FROM entry_sessions WHERE state='pending' AND session_key > ? ORDER BY session_key LIMIT 50",
                    ("",),
                ).fetchall()
                plan = rows[0][0][0]
                encoded = json.dumps(plan)
                assert "idx_entry_sessions_state_key" in encoded
                print(
                    json.dumps(
                        {
                            "schema": conn.execute(
                                "SELECT version FROM _schema_version"
                            ).fetchone()[0],
                            "concurrent_revoke_renew_rounds": 30,
                            "history_rows": 20000,
                            "pending_scan_plan": plan,
                        },
                        ensure_ascii=False,
                    )
                )
            stores[0].finish_revoke(token)
    finally:
        for pool in pools:
            pool.close()


if __name__ == "__main__":
    main()
