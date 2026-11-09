"""Policy writes against a second, independent PostgreSQL writer.

Gated like ``test_expert_assignment_postgresql.py`` (whose helpers this reuses): set
``OCTOP_TEST_DATABASE_URL`` to a dedicated database; the schema is dropped and recreated.

Every policy write locks the user row before inserting a policy row, so two writers on one
account queue on that row instead of waiting on each other's insert.
"""

from __future__ import annotations

import threading
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from tests.integration.test_expert_assignment_postgresql import (
    WAIT,
    _finish,
    _policy,
    _Run,
    _services,
    _wait_blocked_by,
)
from tests.integration.test_expert_assignment_postgresql import pool as _pool
from tests.support.postgresql import requires_postgresql

pytestmark = [requires_postgresql, pytest.mark.postgresql]
pool = _pool


class _PausingMerge:
    """Wraps the policy repo; an in-transaction merge pauses after its first key."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.held = threading.Event()
        self.release = threading.Event()
        self.backend_pid: int | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def merge_in_transaction(self, conn: Any, user_id: int, updates: dict[str, str | None]) -> None:
        items = list(updates.items())
        self._inner.merge_in_transaction(conn, user_id, dict(items[:1]))
        self.backend_pid = int(conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
        self.held.set()
        assert self.release.wait(WAIT), "test never released the native write"
        self._inner.merge_in_transaction(conn, user_id, dict(items[1:]))


def _native(pool: Any, policies: Any = None) -> Any:
    """The admin API's resource-policy write, on its own connection."""
    from octop.infra.users.manager import UserManager

    services = _services(pool, policies)
    services.config = SimpleNamespace(login_max_attempts=5, login_lockout_seconds=60)
    return UserManager(services)


def test_first_login_policies_and_native_write_queue_on_the_user(pool: Any) -> None:
    """LDAP and SSO first logins commit the user, then write the role's policies with a plain
    merge; an admin's native write may already hold that user. Both insert token_quota, so
    both must queue on the user row, not wait on each other's insert."""
    import asyncio

    from octop.infra.users.resource_policy import POLICY_TOKEN_QUOTA

    services = _services(pool)
    username = f"user-{uuid.uuid4().hex[:8]}"
    uid = services.user_repo.create(username=username, password_hash="h", role="user")
    paused = _PausingMerge(services.user_policy_repo)
    manager = _native(pool, paused)
    writer = _Run(
        lambda: asyncio.run(
            manager.set_resource_policy(username, workspace_root_dir=None, token_quota=7)
        )
    )
    first_login = _Run(lambda: services.user_policy_repo.merge(uid, {POLICY_TOKEN_QUOTA: "1000"}))
    writer.start()
    assert paused.held.wait(10), "native write never took the user"
    try:
        first_login.start()
        _wait_blocked_by(services, paused, 1)
    finally:
        paused.release.set()
    _finish(writer, first_login)
    assert writer.error is None and first_login.error is None
    assert _policy(services, uid, POLICY_TOKEN_QUOTA) == "1000"
