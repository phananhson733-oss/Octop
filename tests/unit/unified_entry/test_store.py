"""Reopening and concurrent stale writes must not revive revoked entry sessions."""

import pytest
from cryptography.fernet import Fernet

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.unified_entry.store import Binding, EntryStore, Session
from octop.infra.utils.paths import PathLayout


def test_reopen_preserves_identity_pending_revocation_and_encryption(tmp_path):
    paths = PathLayout(tmp_path)
    key = Fernet.generate_key()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    store = EntryStore(services, key)
    binding = Binding("global-1", "customer", "original", 7)
    store.register("customer/original", binding)
    token = store.create("customer/original", "backend-sensitive-token", 3600)
    independent = store.create("customer/original", "another-sensitive-token", 3600)
    stale = store.read(token)
    assert stale is not None
    store.begin_revoke(token)
    db.close()

    reopened = SqlitePool(paths.db)
    try:
        services = build_shared_services(db=reopened, paths=paths, config=OctopConfig())
        worker = EntryStore(services, key)
        assert worker.binding("customer/original") == binding
        assert worker.read(token) is None
        assert worker.read(token, pending=True).backend_token == "backend-sensitive-token"
        assert worker.read(independent) is not None
        # Simulate refresh that read before another worker revoked the session.
        worker._save(token, Session(stale.alias, stale.binding, "late-renewal", stale.expires_at))
        assert worker.read(token) is None
        worker.finish_revoke(token)
        worker._save(token, stale)
        assert worker.read(token, pending=True) is None
        # A wrong encryption key rejects without destroying recoverable state.
        assert EntryStore(services, Fernet.generate_key()).read(independent) is None
        assert worker.read(independent) is not None
        with reopened.connect() as conn:
            data = repr(
                [dict(row) for row in conn.execute("SELECT * FROM entry_sessions").fetchall()]
            )
        assert "backend-sensitive-token" not in data
        assert "another-sensitive-token" not in data
        assert independent not in data
        assert key.decode() not in data
        with pytest.raises(ValueError, match="conflict"):
            worker.register("customer/original", Binding("other", "internal", "admin", 1))
        assert worker.binding("customer/original") == binding
        other = worker.create("customer/original", "other-token", 3600)
        worker.register("internal/admin", Binding("admin-identity", "internal", "admin", 1))
        privileged = worker.create("internal/admin", "privileged-backend", 3600)
        source_key = worker._key("session", privileged)
        target_key = worker._key("session", other)
        ciphertext = services.repos.entry_session_repo.get(source_key)[1]
        assert ciphertext is not None
        services.repos.entry_session_repo.renew(target_key, ciphertext)
        assert worker.read(other) is None
        assert worker.read(privileged).binding.identity_id == "admin-identity"
        assert worker.read(independent) is not None
        worker.disable(binding.identity_id)
        assert worker.read(independent) is None
        assert worker.binding("customer/original") is None
    finally:
        reopened.close()


def test_watermark_repair_preserves_existing_account_and_pending_session(tmp_path):
    paths = PathLayout(tmp_path)
    db = SqlitePool(paths.db)
    try:
        run_migrations(db)
        services = build_shared_services(db=db, paths=paths, config=OctopConfig())
        uid = services.user_repo.create(username="preserve-me", role="user")
        with db.transaction() as conn:
            conn.execute("DROP TABLE entry_sessions")
        run_migrations(db)
        assert services.user_repo.get(uid).username == "preserve-me"
        services.repos.entry_session_repo.create("key", "ciphertext")
        services.repos.entry_session_repo.revoke("key")
        run_migrations(db)
        assert services.repos.entry_session_repo.get("key") == ("pending", "ciphertext")
    finally:
        db.close()


def test_storage_scope_includes_instance_and_account_not_only_linked_identity():
    assert (
        Binding("same-person", "internal", "user", 7).storage_scope
        != Binding("same-person", "customer", "user", 7).storage_scope
    )
    assert (
        Binding("same-person", "internal", "user", 7).storage_scope
        != Binding("same-person", "internal", "other", 8).storage_scope
    )
    assert (
        Binding("same-person", "internal", "renamed", 7).storage_scope
        == Binding("same-person", "internal", "user", 7).storage_scope
    )
