"""Unified-entry control writes against a second, independent PostgreSQL writer.

Gated like ``test_expert_assignment_postgresql.py`` (whose helpers this reuses): set
``OCTOP_TEST_DATABASE_URL`` to a dedicated database; the schema is dropped and recreated.

What these show on PostgreSQL: a newer operation's claim and an older operation's write on
the same account serialize on the control record row, and whichever runs second sees the
other's outcome: a write of the superseded operation is refused, never applied on top. The
entry's operation table gives each account one version per change.
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
    _PausingPolicies,
    _policy,
    _Run,
    _services,
    _wait_blocked_by,
)
from tests.integration.test_expert_assignment_postgresql import pool as _pool
from tests.support.postgresql import requires_postgresql

pytestmark = [requires_postgresql, pytest.mark.postgresql]
pool = _pool


def _record(operation_id: str, version: int) -> Any:
    from octop.infra.users.control_grants import COMPONENTS, ComponentResult, ControlRecord

    return ControlRecord(
        operation_id=operation_id,
        version=version,
        digest="d" * 64,
        state="applying",
        components={name: ComponentResult("pending") for name in COMPONENTS},
        role_change=False,
        updated_at=0,
    )


def _managed_user(pool: Any) -> tuple[Any, int, str]:
    from octop.infra.users.expert_assignment import POLICY_CONTROL_RECORD
    from octop.infra.users.resource_policy import POLICY_MAX_AGENTS

    services = _services(pool)
    uid = services.user_repo.create(
        username=f"user-{uuid.uuid4().hex[:8]}", password_hash="h", role="user"
    )
    first = _record("op-first", 1).encode()
    services.user_policy_repo.merge(uid, {POLICY_CONTROL_RECORD: first, POLICY_MAX_AGENTS: "1"})
    return services, uid, first


def test_older_operation_write_waits_for_newer_claim_then_is_refused(pool: Any) -> None:
    from octop.infra.errors import ErrorCode, OctopError
    from octop.infra.users.control_grants import _claim
    from octop.infra.users.expert_assignment import (
        POLICY_CONTROL_RECORD,
        ControlActor,
        _record_change,
    )
    from octop.infra.users.resource_policy import POLICY_MAX_AGENTS

    services, uid, first = _managed_user(pool)
    paused = _PausingPolicies(services.user_policy_repo)
    claim = _Run(lambda: _claim(_services(pool, paused), uid, first, _record("op-second", 2), "cp"))
    claim.start()
    assert paused.locked.wait(10), "claim never reached its policy lock"
    older = _Run(
        lambda: _record_change(
            services,
            ControlActor("cp", "op-first"),
            uid,
            {POLICY_MAX_AGENTS: "7"},
            "user.set_max_agents",
            None,
        )
    )
    try:
        older.start()
        _wait_blocked_by(services, paused, 1)
    finally:
        paused.release.set()
    _finish(claim, older)
    assert claim.error is None
    assert isinstance(older.error, OctopError)
    assert older.error.code == ErrorCode.CONTROL_OPERATION_CONFLICT
    assert _policy(services, uid, POLICY_MAX_AGENTS) == "1"
    assert '"op-second"' in (_policy(services, uid, POLICY_CONTROL_RECORD) or "")


def test_newer_claim_waits_for_older_write_and_still_takes_over(pool: Any) -> None:
    from octop.infra.users.control_grants import _claim
    from octop.infra.users.expert_assignment import (
        POLICY_CONTROL_RECORD,
        ControlActor,
        _record_change,
    )
    from octop.infra.users.resource_policy import POLICY_MAX_AGENTS

    services, uid, first = _managed_user(pool)
    paused = _PausingPolicies(services.user_policy_repo)
    older = _Run(
        lambda: _record_change(
            _services(pool, paused),
            ControlActor("cp", "op-first"),
            uid,
            {POLICY_MAX_AGENTS: "7"},
            "user.set_max_agents",
            None,
        )
    )
    older.start()
    assert paused.locked.wait(10), "write never reached its policy lock"
    claim = _Run(lambda: _claim(services, uid, first, _record("op-second", 2), "cp"))
    try:
        claim.start()
        _wait_blocked_by(services, paused, 1)
    finally:
        paused.release.set()
    _finish(older, claim)
    assert older.error is None and claim.error is None
    assert _policy(services, uid, POLICY_MAX_AGENTS) == "7"
    assert '"op-second"' in (_policy(services, uid, POLICY_CONTROL_RECORD) or "")


def test_native_grant_write_waits_for_claim_then_is_refused(pool: Any) -> None:
    """A native admin write racing the first claim sees the account as managed."""
    from octop.infra.errors import ErrorCode, OctopError
    from octop.infra.users.control_grants import _claim
    from octop.infra.users.expert_assignment import _record_change
    from octop.infra.users.resource_policy import POLICY_MAX_AGENTS

    services = _services(pool)
    tag = uuid.uuid4().hex[:8]
    admin = services.user_repo.create(username=f"admin-{tag}", password_hash="h", role="admin")
    uid = services.user_repo.create(username=f"user-{tag}", password_hash="h", role="user")
    paused = _PausingPolicies(services.user_policy_repo)
    claim = _Run(lambda: _claim(_services(pool, paused), uid, None, _record("op-first", 1), "cp"))
    claim.start()
    assert paused.locked.wait(10), "claim never reached its policy lock"
    native = _Run(
        lambda: _record_change(
            services, admin, uid, {POLICY_MAX_AGENTS: "9"}, "user.set_max_agents", None
        )
    )
    try:
        native.start()
        _wait_blocked_by(services, paused, 1)
    finally:
        paused.release.set()
    _finish(claim, native)
    assert claim.error is None
    assert isinstance(native.error, OctopError)
    assert native.error.code == ErrorCode.ACCOUNT_CENTRALLY_MANAGED
    assert _policy(services, uid, POLICY_MAX_AGENTS) is None


def test_role_policies_wait_for_a_native_policy_write_instead_of_deadlocking(pool: Any) -> None:
    """A native resource-policy write holds ``workspace_root_dir`` and then wants
    ``token_quota``; the role component's policy write must queue behind it in that order."""
    from octop.infra.users.control_grants import _finish_role
    from octop.infra.users.expert_assignment import ControlActor
    from octop.infra.users.resource_policy import (
        POLICY_MAX_AGENTS,
        POLICY_TOKEN_QUOTA,
        POLICY_WORKSPACE_ROOT_DIR,
    )

    services, uid, _first = _managed_user(pool)
    services.user_policy_repo.merge(
        uid, {POLICY_WORKSPACE_ROOT_DIR: "/srv/a", POLICY_TOKEN_QUOTA: "9"}
    )
    holding, release = threading.Event(), threading.Event()
    native_pid = SimpleNamespace(backend_pid=None)

    def native() -> None:
        # The statements UserManager.set_resource_policy runs, in its order, paused between.
        with pool.transaction() as conn:
            services.user_policy_repo.merge_in_transaction(
                conn, uid, {POLICY_WORKSPACE_ROOT_DIR: None}
            )
            native_pid.backend_pid = int(
                conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            )
            holding.set()
            assert release.wait(WAIT), "test never released the native write"
            services.user_policy_repo.merge_in_transaction(conn, uid, {POLICY_TOKEN_QUOTA: None})

    updates = {POLICY_WORKSPACE_ROOT_DIR: None, POLICY_TOKEN_QUOTA: "1000", POLICY_MAX_AGENTS: None}
    writer = _Run(native)
    role = _Run(lambda: _finish_role(services, uid, ControlActor("cp", "op-first"), updates))
    writer.start()
    assert holding.wait(10), "native write never took its first row"
    try:
        role.start()
        _wait_blocked_by(services, native_pid, 1)
    finally:
        release.set()
    _finish(writer, role)
    assert writer.error is None and role.error is None
    assert _policy(services, uid, POLICY_TOKEN_QUOTA) == "1000"
    assert _policy(services, uid, POLICY_WORKSPACE_ROOT_DIR) is None


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


def _role_updates() -> dict[str, str | None]:
    from octop.infra.users.resource_policy import (
        POLICY_MAX_AGENTS,
        POLICY_TOKEN_QUOTA,
        POLICY_WORKSPACE_ROOT_DIR,
    )

    return {POLICY_WORKSPACE_ROOT_DIR: None, POLICY_TOKEN_QUOTA: "1000", POLICY_MAX_AGENTS: None}


def test_native_write_holding_a_policy_row_then_inserting_queues_on_the_user(pool: Any) -> None:
    """The native write holds an existing workspace row, then inserts token_quota, whose
    foreign key check needs the user row the role component would otherwise be holding."""
    import asyncio

    from octop.infra.users.control_grants import _finish_role
    from octop.infra.users.expert_assignment import ControlActor
    from octop.infra.users.resource_policy import POLICY_TOKEN_QUOTA, POLICY_WORKSPACE_ROOT_DIR

    updates = _role_updates()
    services, uid, _first = _managed_user(pool)
    username = services.user_repo.get(uid).username
    services.user_policy_repo.merge(uid, {POLICY_WORKSPACE_ROOT_DIR: "/srv/a"})
    paused = _PausingMerge(services.user_policy_repo)
    manager = _native(pool, paused)
    writer = _Run(
        lambda: asyncio.run(
            manager.set_resource_policy(username, workspace_root_dir=None, token_quota=7)
        )
    )
    role = _Run(lambda: _finish_role(services, uid, ControlActor("cp", "op-first"), updates))
    writer.start()
    assert paused.held.wait(10), "native write never took its first row"
    try:
        role.start()
        _wait_blocked_by(services, paused, 1)
    finally:
        paused.release.set()
    _finish(writer, role)
    assert writer.error is None and role.error is None
    assert _policy(services, uid, POLICY_TOKEN_QUOTA) == "1000"


def test_native_insert_waits_for_role_policies_holding_the_user(pool: Any) -> None:
    """The role component holds the user first; a native write then inserts token_quota."""
    import asyncio

    from octop.infra.users.control_grants import _finish_role
    from octop.infra.users.expert_assignment import ControlActor
    from octop.infra.users.resource_policy import POLICY_TOKEN_QUOTA

    updates = _role_updates()
    services, uid, _first = _managed_user(pool)
    username = services.user_repo.get(uid).username
    held = _PausingPolicies(services.user_policy_repo)
    role = _Run(
        lambda: _finish_role(_services(pool, held), uid, ControlActor("cp", "op-first"), updates)
    )
    role.start()
    assert held.locked.wait(10), "role component never took its locks"
    writer = _Run(lambda: asyncio.run(_native(pool).set_resource_policy(username, token_quota=7)))
    try:
        writer.start()
        _wait_blocked_by(services, held, 1)
    finally:
        held.release.set()
    _finish(role, writer)
    assert writer.error is None and role.error is None
    assert _policy(services, uid, POLICY_TOKEN_QUOTA) == "7"


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


def test_entry_operations_take_one_version_per_change(pool: Any) -> None:
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.repos.entry_admin_operations import EntryAdminOperationRepo

    with pool.connect() as conn:
        conn.execute("DROP TABLE entry_admin_operations")
        conn.execute("COMMIT")
    run_migrations(pool)  # repairs the table on an already-watermarked database
    repo = EntryAdminOperationRepo(pool)
    first = repo.create_next("op-a", "customer/x", "customer", 7, "{}")
    second = repo.create_next("op-b", "customer/x", "customer", 7, "{}")
    other = repo.create_next("op-c", "internal/x", "internal", 7, "{}")
    assert (first.version, second.version, other.version) == (1, 2, 1)
    with pytest.raises(Exception):  # noqa: B017 - the driver's unique violation
        repo.create_next("op-a", "customer/x", "customer", 7, "{}")

    repo.record("op-a", "applied", '{"state": "applied"}', None)
    repo.record("op-a", "pending", None, None)
    assert repo.get("op-a").state == "applied"
    assert repo.requeue("op-a") is False
    repo.record("op-b", "partial", None, None)
    assert repo.requeue("op-b") is True
    instances = ["customer", "internal"]
    assert [row.operation_id for row in repo.pending(10, instances)] == ["op-b", "op-c"]
    assert [row.operation_id for row in repo.pending(10, instances, second.pk)] == ["op-c"]
    assert [row.operation_id for row in repo.pending(10, ["internal"])] == ["op-c"]

    # Concurrent submits for one account never share a version.
    start = threading.Barrier(4)
    made: list[int] = []
    errors: list[BaseException] = []

    def submit(n: int) -> None:
        start.wait()
        try:
            made.append(repo.create_next(f"op-race-{n}", "customer/x", "customer", 7, "{}").version)
        except Exception as exc:  # noqa: BLE001 - a lost race is the expected failure
            errors.append(exc)

    threads = [threading.Thread(target=submit, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert made and len(made) + len(errors) == 4
    assert len(set(made)) == len(made)
    with pool.connect() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS c, COUNT(DISTINCT version) AS v FROM entry_admin_operations "
            "WHERE instance = 'customer' AND user_id = 7"
        ).fetchone()
    assert int(rows["c"]) == int(rows["v"]) == 2 + len(made)
