"""Expert assignment commits against a second, independent PostgreSQL writer.

The in-process ``asyncio.Lock`` in ``ExpertAssignments`` only serializes one server
process. These tests run the final assignment commit (``_record_change``) in one thread
and pause it while it holds its row locks, then let a second connection write the same
user's quota, assignment or agents, the way another worker process or the admin quota
page would.

Gated like ``test_postgresql_control_plane.py``: set ``OCTOP_TEST_DATABASE_URL`` to a
dedicated database (the schema is dropped and recreated).

What these show on PostgreSQL: a second writer of the same user's quota, assignment or
agents waits for the assignment to commit. Writes to an existing policy row wait on that
row; inserts (a new ``max_agents`` row, a new agent) wait on the user row, because their
``user_id`` foreign key takes ``FOR KEY SHARE`` on it and ``assignment_principal`` holds it
``FOR UPDATE``.

Waiting is proven from the database side: the tests poll ``pg_blocking_pids`` until the
second writer is blocked by the paused assignment's backend, not by elapsed time.

Installation reservations move by compare-and-set under the same locks: two processes
cannot both claim one installation, and of a final commit and a discard racing on the same
``ready:`` reservation exactly one lands. Expert creation counts the quota after locking the
user row, in its insert transaction, so concurrent creates and a create racing an
assignment cannot exceed the committed limit.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from tests.support.postgresql import requires_postgresql

pytestmark = [requires_postgresql, pytest.mark.postgresql]

WAIT = 10.0


@pytest.fixture
def pool() -> Iterator[Any]:
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import PostgresPool

    pg = PostgresPool(os.environ["OCTOP_TEST_DATABASE_URL"])
    try:
        with pg.connect() as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
            conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
            conn.execute("COMMIT")
        run_migrations(pg)
        yield pg
    finally:
        pg.close()


def _services(pool: Any, policies: Any = None) -> SimpleNamespace:
    from octop.infra.db.repos.agents import AgentRepo
    from octop.infra.db.repos.audit import AuditRepo
    from octop.infra.db.repos.user_policies import UserPolicyRepo
    from octop.infra.db.repos.users import UserRepo

    return SimpleNamespace(
        db=pool,
        user_repo=UserRepo(pool),
        user_policy_repo=policies or UserPolicyRepo(pool),
        agent_repo=AgentRepo(pool),
        audit_repo=AuditRepo(pool),
    )


class _PausingPolicies:
    """Wraps the policy repo; ``lock_for_assignment`` holds its locks until released."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.locked = threading.Event()
        self.release = threading.Event()
        self.backend_pid: int | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def lock_for_assignment(self, conn: Any, user_id: int, names: tuple[str, ...]) -> Any:
        rows = self._inner.lock_for_assignment(conn, user_id, names)
        self.backend_pid = int(conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
        self.locked.set()
        assert self.release.wait(WAIT), "test never released the paused assignment"
        return rows


def _case(
    pool: Any, *, limit: int | None, install_new: bool = True
) -> tuple[SimpleNamespace, int, int, str]:
    """A user with ``old`` assigned and a verified, unconfirmed copy of ``new``."""
    from octop.infra.users.expert_access import (
        INSTALLING_EXPERT_PREFIX,
        POLICY_ASSIGNED_EXPERTS,
        READY_PREFIX,
    )
    from octop.infra.users.resource_policy import POLICY_MAX_AGENTS

    services = _services(pool)
    tag = uuid.uuid4().hex[:8]
    admin = services.user_repo.create(username=f"admin-{tag}", password_hash="h", role="admin")
    uid = services.user_repo.create(username=f"user-{tag}", password_hash="h", role="user")
    services.agent_repo.create(
        agent_id=f"old-{tag}", user_id=uid, name="old", published_expert_id="old"
    )
    new_id = f"new-{tag}"
    if install_new:
        services.agent_repo.create(
            agent_id=new_id, user_id=uid, name="new", published_expert_id="new"
        )
    seed: dict[str, str | None] = {
        POLICY_ASSIGNED_EXPERTS: '["old"]',
        INSTALLING_EXPERT_PREFIX + "new": READY_PREFIX + new_id,
    }
    if limit is not None:
        seed[POLICY_MAX_AGENTS] = str(limit)
    services.user_policy_repo.merge(uid, seed)
    return services, admin, uid, new_id


def _revision(services: SimpleNamespace, uid: int) -> str:
    """What the preview step would hand the admin right now."""
    from octop.infra.db.repos.user_roles import UserRoleRepo
    from octop.infra.users.expert_access import (
        POLICY_ALLOWED_EXPERT_TEMPLATES,
        POLICY_ASSIGNED_EXPERTS,
    )
    from octop.infra.users.expert_assignment import _authority_revision
    from octop.infra.users.resource_grants import grants_from_role
    from octop.infra.users.resource_policy import POLICY_MAX_AGENTS

    with services.db.transaction() as conn:
        role = UserRoleRepo(services.db).lock_for_assignment(conn, "user")
        rows = services.user_policy_repo.lock_for_assignment(
            conn, uid, (POLICY_MAX_AGENTS, POLICY_ASSIGNED_EXPERTS)
        )
        return str(
            _authority_revision(
                "user", grants_from_role(role, POLICY_ALLOWED_EXPERT_TEMPLATES), rows
            )
        )


def _final_updates(raise_to: int | None = None) -> dict[str, str | None]:
    from octop.infra.users.expert_access import INSTALLING_EXPERT_PREFIX, POLICY_ASSIGNED_EXPERTS
    from octop.infra.users.resource_policy import POLICY_MAX_AGENTS

    updates: dict[str, str | None] = {
        INSTALLING_EXPERT_PREFIX + "new": None,
        POLICY_ASSIGNED_EXPERTS: '["new", "old"]',
    }
    if raise_to is not None:
        updates[POLICY_MAX_AGENTS] = str(raise_to)
    return updates


class _Run(threading.Thread):
    def __init__(self, target: Any) -> None:
        super().__init__(daemon=True)
        self._fn = target
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self._fn()
        except BaseException as exc:  # noqa: BLE001 - surfaced through ``error``
            self.error = exc


def _paused_assignment(
    pool: Any,
    services: SimpleNamespace,
    admin: int,
    uid: int,
    revision: str | None,
    updates: dict[str, str | None],
    *,
    action: str = "user.assign_experts",
    expect: dict[str, str | None] | None = None,
) -> tuple[_Run, _PausingPolicies]:
    from octop.infra.users.expert_assignment import _record_change

    paused = _PausingPolicies(services.user_policy_repo)
    first = _services(pool, paused)
    run = _Run(lambda: _record_change(first, admin, uid, updates, action, revision, expect=expect))
    run.start()
    assert paused.locked.wait(WAIT), "assignment never reached its policy lock"
    return run, paused


def _wait_blocked_by(services: SimpleNamespace, paused: Any, waiters: int) -> None:
    """Poll until ``waiters`` sessions are blocked by the paused assignment's backend.

    A session queued behind another waiter for the same row lock reports that waiter as its
    blocker, so the count follows the chain back to the paused backend.
    """
    assert paused.backend_pid is not None
    deadline = time.monotonic() + WAIT
    seen = 0
    while time.monotonic() < deadline:
        with services.db.connect() as conn:
            row = conn.execute(
                """
                WITH RECURSIVE blocked(pid) AS (
                  SELECT pid FROM pg_stat_activity WHERE ? = ANY(pg_blocking_pids(pid))
                  UNION
                  SELECT a.pid FROM pg_stat_activity a
                  JOIN blocked b ON b.pid = ANY(pg_blocking_pids(a.pid))
                )
                SELECT COUNT(*) AS c FROM blocked
                """,
                (paused.backend_pid,),
            ).fetchone()
        seen = int(row["c"])
        if seen >= waiters:
            return
        time.sleep(0.05)
    pytest.fail(f"expected {waiters} session(s) blocked by the assignment, saw {seen}")


def _finish(*runs: _Run) -> None:
    for run in runs:
        run.join(WAIT)
        assert not run.is_alive(), "writer did not finish after the assignment committed"


def _policy(services: SimpleNamespace, uid: int, name: str) -> str | None:
    row = services.user_policy_repo.get(uid, name)
    return row.value if row and row.enabled else None


def _assign_audits(services: SimpleNamespace, uid: int) -> int:
    with services.db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM audit_log WHERE action = ? AND target = ?",
            ("user.assign_experts", str(uid)),
        ).fetchone()
    return int(row["c"])


def _expert_count(services: SimpleNamespace, uid: int) -> int:
    return int(services.agent_repo.count_by_user(uid, kind="expert"))


def test_quota_write_waits_for_assignment_holding_existing_row(pool: Any) -> None:
    """Existing ``max_agents`` row: the other admin's write blocks, then lands on top."""
    from octop.infra.users.expert_access import POLICY_ASSIGNED_EXPERTS
    from octop.infra.users.resource_policy import POLICY_MAX_AGENTS

    services, admin, uid, _ = _case(pool, limit=1)
    revision = _revision(services, uid)
    first, paused = _paused_assignment(pool, services, admin, uid, revision, _final_updates(2))
    other = _Run(lambda: services.user_policy_repo.merge(uid, {POLICY_MAX_AGENTS: "5"}))
    try:
        other.start()
        _wait_blocked_by(services, paused, 1)
    finally:
        paused.release.set()
    _finish(first, other)
    assert first.error is None and other.error is None
    assert _policy(services, uid, POLICY_MAX_AGENTS) == "5"
    assert json.loads(_policy(services, uid, POLICY_ASSIGNED_EXPERTS) or "[]") == ["new", "old"]
    assert _assign_audits(services, uid) == 1


def test_second_process_assignment_waits_then_conflicts(pool: Any) -> None:
    """Two processes applying the same preview: the second waits, then 409s.

    Both use the same admin and principals are locked in id order, so the second may wait
    on the admin's users row rather than the target's; either way it waits for the first.
    """
    from octop.infra.errors import OctopError
    from octop.infra.users.expert_assignment import _record_change
    from octop.infra.users.resource_policy import POLICY_MAX_AGENTS

    services, admin, uid, _ = _case(pool, limit=1)
    revision = _revision(services, uid)
    first, paused = _paused_assignment(pool, services, admin, uid, revision, _final_updates(2))
    other = _Run(
        lambda: _record_change(
            services, admin, uid, _final_updates(3), "user.assign_experts", revision
        )
    )
    try:
        other.start()
        _wait_blocked_by(services, paused, 1)
    finally:
        paused.release.set()
    _finish(first, other)
    assert first.error is None
    assert isinstance(other.error, OctopError) and other.error.status == 409
    assert _policy(services, uid, POLICY_MAX_AGENTS) == "2"
    assert _assign_audits(services, uid) == 1


def test_inserted_limit_waits_for_assignment_without_existing_row(pool: Any) -> None:
    """No ``max_agents`` row, so the policy lock has no limit row to hold.

    The insert still waits: a policy merge locks the user row first, the agent insert's
    foreign key takes ``FOR KEY SHARE`` on it, and the assignment holds ``FOR UPDATE`` on it
    from ``assignment_principal``. The lower limit lands after the assignment, the same as an
    admin lowering it a moment later.
    """
    from octop.infra.users.resource_policy import POLICY_MAX_AGENTS

    services, admin, uid, _ = _case(pool, limit=None)
    revision = _revision(services, uid)
    first, paused = _paused_assignment(pool, services, admin, uid, revision, _final_updates())
    limit = _Run(lambda: services.user_policy_repo.merge(uid, {POLICY_MAX_AGENTS: "1"}))
    agent = _Run(lambda: services.agent_repo.create(agent_id=f"x-{uid}", user_id=uid, name="x"))
    try:
        limit.start()
        agent.start()
        _wait_blocked_by(services, paused, 2)
    finally:
        paused.release.set()
    _finish(first, limit, agent)
    for run in (first, limit, agent):
        assert run.error is None
    assert _policy(services, uid, POLICY_MAX_AGENTS) == "1"
    assert _assign_audits(services, uid) == 1


def _reservation_key(template: str) -> str:
    from octop.infra.users.expert_access import INSTALLING_EXPERT_PREFIX

    return str(INSTALLING_EXPERT_PREFIX + template)


def test_two_processes_cannot_claim_the_same_installation(pool: Any) -> None:
    """Both saw no reservation for ``third``; the second claim waits, then 409s."""
    from octop.infra.errors import OctopError
    from octop.infra.users.expert_assignment import _record_change

    services, admin, uid, _ = _case(pool, limit=None)
    key = _reservation_key("third")
    revision = _revision(services, uid)
    action = "user.install_assigned_expert"
    first, paused = _paused_assignment(
        pool, services, admin, uid, revision, {key: "agent-a"}, action=action, expect={key: None}
    )
    other = _Run(
        lambda: _record_change(
            services, admin, uid, {key: "agent-b"}, action, revision, expect={key: None}
        )
    )
    try:
        other.start()
        _wait_blocked_by(services, paused, 1)
    finally:
        paused.release.set()
    _finish(first, other)
    assert first.error is None
    assert isinstance(other.error, OctopError) and other.error.status == 409
    assert _policy(services, uid, key) == "agent-a"


def _discard_mark(services: SimpleNamespace, admin: int, uid: int, new_id: str) -> None:
    from octop.infra.users.expert_access import DISCARDING_PREFIX, READY_PREFIX
    from octop.infra.users.expert_assignment import _record_change

    key = _reservation_key("new")
    _record_change(
        services,
        admin,
        uid,
        {key: DISCARDING_PREFIX + new_id},
        "user.expert_install_discarding",
        None,
        expect={key: READY_PREFIX + new_id},
    )


def test_final_commit_first_makes_concurrent_discard_conflict(pool: Any) -> None:
    """The confirm commits; the discard that read ``ready:`` before it then 409s."""
    from octop.infra.errors import OctopError
    from octop.infra.users.expert_access import POLICY_ASSIGNED_EXPERTS, READY_PREFIX

    services, admin, uid, new_id = _case(pool, limit=None)
    key = _reservation_key("new")
    first, paused = _paused_assignment(
        pool,
        services,
        admin,
        uid,
        _revision(services, uid),
        _final_updates(),
        expect={key: READY_PREFIX + new_id},
    )
    discard = _Run(lambda: _discard_mark(services, admin, uid, new_id))
    try:
        discard.start()
        _wait_blocked_by(services, paused, 1)
    finally:
        paused.release.set()
    _finish(first, discard)
    assert first.error is None
    assert isinstance(discard.error, OctopError) and discard.error.status == 409
    assert _policy(services, uid, key) is None
    assert json.loads(_policy(services, uid, POLICY_ASSIGNED_EXPERTS) or "[]") == ["new", "old"]
    assert _assign_audits(services, uid) == 1


def test_discard_first_makes_concurrent_final_commit_conflict(pool: Any) -> None:
    """The discard marks the key; the confirm that read ``ready:`` before it then 409s."""
    from octop.infra.errors import OctopError
    from octop.infra.users.expert_access import (
        DISCARDING_PREFIX,
        POLICY_ASSIGNED_EXPERTS,
        READY_PREFIX,
    )
    from octop.infra.users.expert_assignment import _record_change

    services, admin, uid, new_id = _case(pool, limit=None)
    key = _reservation_key("new")
    revision = _revision(services, uid)
    first, paused = _paused_assignment(
        pool,
        services,
        admin,
        uid,
        None,
        {key: DISCARDING_PREFIX + new_id},
        action="user.expert_install_discarding",
        expect={key: READY_PREFIX + new_id},
    )
    confirm = _Run(
        lambda: _record_change(
            services,
            admin,
            uid,
            _final_updates(),
            "user.assign_experts",
            revision,
            expect={key: READY_PREFIX + new_id},
        )
    )
    try:
        confirm.start()
        _wait_blocked_by(services, paused, 1)
    finally:
        paused.release.set()
    _finish(first, confirm)
    assert first.error is None
    assert isinstance(confirm.error, OctopError) and confirm.error.status == 409
    assert _policy(services, uid, key) == DISCARDING_PREFIX + new_id
    assert json.loads(_policy(services, uid, POLICY_ASSIGNED_EXPERTS) or "[]") == ["old"]
    assert _assign_audits(services, uid) == 0


def _create_expert(
    services: SimpleNamespace,
    uid: int,
    agent_id: str,
    reservation: tuple[str, str] | None = None,
) -> None:
    """What ``AgentManager.create`` commits for an expert: count and insert in one transaction."""
    from octop.infra.users.resource_policy import create_expert_within_quota

    create_expert_within_quota(
        services,
        uid,
        lambda conn: services.agent_repo.create(
            agent_id=agent_id, user_id=uid, name=agent_id, conn=conn
        ),
        reservation=reservation,
    )


def test_two_processes_creating_experts_cannot_both_take_the_last_slot(pool: Any) -> None:
    """One slot left: the second create waits for the first, then counts it and refuses."""
    from octop.infra.errors import ErrorCode, OctopError

    services, _, uid, _ = _case(pool, limit=2, install_new=False)
    paused = _PausingPolicies(services.user_policy_repo)
    first = _Run(lambda: _create_expert(_services(pool, paused), uid, f"a-{uid}"))
    second = _Run(lambda: _create_expert(services, uid, f"b-{uid}"))
    try:
        first.start()
        assert paused.locked.wait(WAIT), "first create never reached its quota lock"
        second.start()
        _wait_blocked_by(services, paused, 1)
    finally:
        paused.release.set()
    _finish(first, second)
    assert first.error is None
    assert isinstance(second.error, OctopError)
    assert second.error.code == ErrorCode.AGENT_QUOTA_EXCEEDED
    assert _expert_count(services, uid) == 2


def test_create_waits_for_assignment_and_counts_against_its_limit(pool: Any) -> None:
    """The assignment confirms ``new`` and lowers the limit to 2 of 2; a create that was
    waiting behind it reads the committed limit and refuses."""
    from octop.infra.errors import ErrorCode, OctopError
    from octop.infra.users.resource_policy import POLICY_MAX_AGENTS

    services, admin, uid, _ = _case(pool, limit=3)
    first, paused = _paused_assignment(
        pool, services, admin, uid, _revision(services, uid), _final_updates(2)
    )
    create = _Run(lambda: _create_expert(services, uid, f"late-{uid}"))
    try:
        create.start()
        _wait_blocked_by(services, paused, 1)
    finally:
        paused.release.set()
    _finish(first, create)
    assert first.error is None
    assert isinstance(create.error, OctopError)
    assert create.error.code == ErrorCode.AGENT_QUOTA_EXCEEDED
    assert _policy(services, uid, POLICY_MAX_AGENTS) == "2"
    assert _expert_count(services, uid) == 2


def test_install_insert_waits_for_discard_mark_then_refuses(pool: Any) -> None:
    """An installer that has not inserted yet loses to a discard that marked the key first."""
    from octop.infra.errors import OctopError
    from octop.infra.users.expert_access import DISCARDING_PREFIX

    services, admin, uid, new_id = _case(pool, limit=None, install_new=False)
    key = _reservation_key("new")
    services.user_policy_repo.merge(uid, {key: new_id})
    first, paused = _paused_assignment(
        pool,
        services,
        admin,
        uid,
        None,
        {key: DISCARDING_PREFIX + new_id},
        action="user.expert_install_discarding",
        expect={key: new_id},
    )
    insert = _Run(lambda: _create_expert(services, uid, new_id, (key, new_id)))
    try:
        insert.start()
        _wait_blocked_by(services, paused, 1)
    finally:
        paused.release.set()
    _finish(first, insert)
    assert first.error is None
    assert isinstance(insert.error, OctopError) and insert.error.status == 409
    assert services.agent_repo.get(new_id) is None
    assert _policy(services, uid, key) == DISCARDING_PREFIX + new_id
