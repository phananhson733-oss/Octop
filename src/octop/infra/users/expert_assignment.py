"""Plan an account's expert assignment against its current role, instances and quota."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any
from uuid import uuid4
from weakref import WeakValueDictionary

from pydantic import Field

from octop.infra.db.repos.user_policies import UserPolicyRow
from octop.infra.db.repos.user_roles import UserRoleRepo
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.expert_access import (
    DISCARDING_PREFIX,
    INSTALLING_EXPERT_PREFIX,
    POLICY_ALLOWED_EXPERT_TEMPLATES,
    POLICY_ASSIGNED_EXPERTS,
    READY_PREFIX,
    account_expert_grants,
    parse_reservation,
)
from octop.infra.users.resource_grants import grants_from_role, role_resource_grants
from octop.infra.users.resource_policy import POLICY_MAX_AGENTS, max_agents_of

# An installation that has not reported back (no ``ready:``) within this long is treated as
# dead and may be discarded. Another server process could still be inside it only if a single
# install ran longer than this; its later writes then fail their reservation check.
INSTALL_LEASE_SECONDS = 15 * 60

_STATE_PREFIX = {"installing": "", "ready": READY_PREFIX, "discarding": DISCARDING_PREFIX}

# The unified entry's applied record for this account (JSON). Once it exists the entry is the
# only write source for the role, expert limit and expert list (plan §3.2.5).
POLICY_CONTROL_RECORD = "control_plane_record"


@dataclass(frozen=True)
class ControlActor:
    """The unified entry, acting through a verified signature for one operation.

    Every write it makes re-checks, under the same locks, that the account's control record
    still names ``operation_id``, so a superseded operation can no longer change the account.
    """

    label: str
    operation_id: str


def control_operation_of(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(json.loads(value)["operation_id"])
    except (ValueError, KeyError, TypeError):
        return None


def _grants_write(updates: Mapping[str, str | None]) -> bool:
    """True when ``updates`` would change what the account may use.

    Recording an installation's outcome (``ready:``), marking or clearing a discard grants
    nothing and stays open to native admins; a new claim, the expert list or the limit do not.
    """
    return (
        POLICY_ASSIGNED_EXPERTS in updates
        or POLICY_MAX_AGENTS in updates
        or any(
            key.startswith(INSTALLING_EXPERT_PREFIX)
            and value is not None
            and parse_reservation(value)[0] == "installing"
            for key, value in updates.items()
        )
    )


def assert_not_centrally_managed(services: SharedServices, user_id: int) -> None:
    """Refuse a native role, limit or expert-list change on an entry-managed account.

    Fails before a multi-step native edit starts. The expert list is also re-checked under
    lock in ``_record_change``; a native role or limit write is not, see the status doc.
    """
    row = services.user_policy_repo.get(user_id, POLICY_CONTROL_RECORD)
    if row is not None and row.enabled:
        raise OctopError(
            ErrorCode.ACCOUNT_CENTRALLY_MANAGED, "account is managed by the unified entry"
        )


@dataclass(frozen=True)
class PendingInstall:
    """An instance reserved for one template and not yet confirmed into the assignment."""

    agent_id: str
    state: str
    since: int
    discardable_at: int

    @property
    def value(self) -> str:
        return _STATE_PREFIX[self.state] + self.agent_id


@dataclass(frozen=True)
class DiscardReceipt:
    """An unconfirmed installation removed: its reservation is cleared and no instance is left."""

    template: str
    agent_id: str
    deleted_instance: Annotated[
        bool,
        Field(
            description=(
                "True only if this call deleted the instance; false if none was ever "
                "inserted or another process removed it first."
            )
        ),
    ]


@dataclass(frozen=True)
class AssignmentPlan:
    user_id: int
    templates: list[str]
    reuse: dict[str, list[str]]
    install: list[str]
    conflicts: dict[str, str]
    current_limit: int | None
    required_capacity: int
    needs_capacity_increase: bool
    revision: str
    snapshot_versions: dict[str, str]
    authority_revision: str
    ready: list[str]
    pending: dict[str, PendingInstall]


def _authority_revision(
    name: str, grants: frozenset[str] | None, rows: dict[str, UserPolicyRow]
) -> str:
    value = [
        name,
        sorted(grants) if grants is not None else None,
        [
            (rows[key].enabled, rows[key].value) if key in rows else None
            for key in (POLICY_MAX_AGENTS, POLICY_ASSIGNED_EXPERTS)
        ],
    ]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def _snapshot_version(row: Any, directory: Path) -> str:
    if not directory.is_dir() or directory.is_symlink():
        raise OctopError(ErrorCode.NOT_FOUND, "template snapshot unavailable")
    files = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise OctopError(
                ErrorCode.EXPERT_ASSIGNMENT_CONFLICT,
                "template snapshot contains a link",
                status=409,
            )
        if path.is_file():
            with path.open("rb") as content:
                files.append(
                    (
                        path.relative_to(directory).as_posix(),
                        hashlib.file_digest(content, "sha256").hexdigest(),
                    )
                )
    return hashlib.sha256(json.dumps([asdict(row), files], sort_keys=True).encode()).hexdigest()


def _stage(
    services: SharedServices, template: str, directory: Path, version: str
) -> tuple[Any, Path]:
    row = services.published_expert_repo.get(template)
    if row is None:
        raise OctopError(ErrorCode.NOT_FOUND, "template no longer published")
    shutil.copytree(Path(services.paths.published_experts_dir) / row.id, directory, symlinks=True)
    if _snapshot_version(row, directory) != version:
        raise OctopError(
            ErrorCode.EXPERT_ASSIGNMENT_CONFLICT, "template snapshot changed", status=409
        )
    return row, directory


def plan_expert_assignment(
    services: SharedServices,
    user_id: int,
    templates: list[str],
    *,
    role_override: str | None = None,
) -> AssignmentPlan:
    """``role_override`` previews the ceiling of a role the account does not hold yet."""
    user = services.user_repo.get(user_id)
    if user is None:
        raise OctopError(ErrorCode.NOT_FOUND, "user not found")
    if user.disabled or user.role == "admin":
        raise OctopError(ErrorCode.FORBIDDEN, "account unavailable for curated assignment")
    desired = list(dict.fromkeys(templates))
    if any(not item or item != item.strip() for item in desired):
        raise OctopError(ErrorCode.FORBIDDEN, "invalid template id", status=400)
    role = role_resource_grants(
        services.db,
        SimpleNamespace(role=role_override or user.role, is_admin=False),
        POLICY_ALLOWED_EXPERT_TEMPLATES,
    )
    all_agents = services.agent_repo.list_by_user(user_id)
    owned = [row for row in all_agents if row.kind == "expert"]
    names = {row.name for row in all_agents}
    versions: dict[str, str] = {}
    policy_rows = {row.name: row for row in services.user_policy_repo.list_for_user(user_id)}
    account_policies = {key: row.value for key, row in policy_rows.items() if row.enabled}
    pending = {
        key.removeprefix(INSTALLING_EXPERT_PREFIX): value
        for key, value in account_policies.items()
        if key.startswith(INSTALLING_EXPERT_PREFIX)
    }
    pending_installs: dict[str, PendingInstall] = {}
    for template, value in pending.items():
        phase, reserved_id = parse_reservation(value)
        since = policy_rows[INSTALLING_EXPERT_PREFIX + template].updated_at
        lease = INSTALL_LEASE_SECONDS if phase == "installing" else 0
        pending_installs[template] = PendingInstall(reserved_id, phase, since, since + lease)
    reuse: dict[str, list[str]] = {}
    install: list[str] = []
    conflicts: dict[str, str] = {}
    ready: list[str] = []
    for template in desired:
        if role is not None and template not in role:
            conflicts[template] = "outside_role"
            continue
        copies = [row for row in owned if row.published_expert_id == template]
        enabled = [row.agent_id for row in copies if row.enabled]
        reservation = pending_installs.get(template)
        if (
            reservation is not None
            and reservation.state != "ready"
            and any(row.agent_id == reservation.agent_id for row in copies)
        ):
            conflicts[template] = (
                "installation_unconfirmed"
                if reservation.state == "installing"
                else "installation_discarding"
            )
        elif enabled:
            reuse[template] = enabled
            if (
                reservation is not None
                and reservation.state == "ready"
                and reservation.agent_id in enabled
            ):
                ready.append(template)
        elif copies:
            conflicts[template] = "existing_copy_disabled"
        else:
            published = services.published_expert_repo.get(template)
            if published is None:
                conflicts[template] = "missing_template"
            elif published.name in names:
                conflicts[template] = "name_in_use"
            else:
                names.add(published.name)
                versions[template] = _snapshot_version(
                    published, Path(services.paths.published_experts_dir) / published.id
                )
                install.append(template)
    limit = max_agents_of(policy_rows.get(POLICY_MAX_AGENTS))
    capacity = len(owned) + len(install)
    authority_revision = _authority_revision(user.role, role, policy_rows)
    state = [
        authority_revision,
        user_id,
        user.role,
        sorted(role) if role is not None else None,
        desired,
        versions,
        account_policies.get(POLICY_ASSIGNED_EXPERTS),
        limit,
        sorted((row.agent_id, row.published_expert_id, row.name, row.enabled) for row in owned),
        conflicts,
        install,
        pending,
    ]
    revision = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
    return AssignmentPlan(
        user_id,
        desired,
        reuse,
        install,
        conflicts,
        limit,
        capacity,
        bool(install or ready) and limit is not None and capacity > limit,
        revision,
        versions,
        authority_revision,
        ready,
        pending_installs,
    )


def _record_change(
    services: SharedServices,
    actor_id: int | ControlActor,
    user_id: int,
    updates: dict[str, str | None],
    action: str,
    authority_revision: str | None,
    *,
    expect: Mapping[str, str | None] | None = None,
    absent: tuple[str, ...] = (),
) -> None:
    """Commit ``updates`` under the locked role and quota rows.

    ``authority_revision=None`` skips only the authority comparison. It is for writes that
    record an installation outcome and grant nothing; actor and target checks still apply.

    ``expect`` maps installing keys to the value each must still hold (``None``: absent or
    cleared), compared under lock. Every reservation transition passes it, so two processes
    cannot both claim, confirm or discard the same installation.

    ``absent`` lists agent IDs that must not exist, checked under the same user-row lock an
    install insert takes: replacing a reservation whose instance was missing at preview
    fails if that installer inserted it since.

    A ``ControlActor`` is authorized by its signature, not an account: it must still own the
    control record. A native admin may not grant anything once that record exists.
    """
    expected = dict(expect or {})
    control = actor_id if isinstance(actor_id, ControlActor) else None
    admin_id = None if isinstance(actor_id, ControlActor) else actor_id
    with services.db.transaction() as conn:
        ids = {user_id} if admin_id is None else {admin_id, user_id}
        principals = {
            key: services.user_repo.assignment_principal(conn, key) for key in sorted(ids)
        }
        target = principals[user_id]
        actor = principals[admin_id] if admin_id is not None else None
        if (
            (admin_id is not None and (not actor or actor[2] or actor[1] != "admin"))
            or not target
            or target[2]
            or target[1] == "admin"
        ):
            raise OctopError(ErrorCode.FORBIDDEN, "assignment authority changed")
        role = UserRoleRepo(services.db).lock_for_assignment(conn, target[1])
        ceiling = grants_from_role(role, POLICY_ALLOWED_EXPERT_TEMPLATES)
        locked = services.user_policy_repo.lock_for_assignment(
            conn,
            user_id,
            (POLICY_CONTROL_RECORD, POLICY_MAX_AGENTS, POLICY_ASSIGNED_EXPERTS, *sorted(expected)),
        )
        record = locked.get(POLICY_CONTROL_RECORD)
        managed = record is not None and record.enabled
        owner = control_operation_of(record.value) if record is not None and managed else None
        if control is not None and owner != control.operation_id:
            raise OctopError(
                ErrorCode.CONTROL_OPERATION_CONFLICT,
                "control operation superseded",
                details={"reason": "superseded"},
            )
        if control is None and managed and _grants_write(updates):
            raise OctopError(
                ErrorCode.ACCOUNT_CENTRALLY_MANAGED, "account is managed by the unified entry"
            )
        if authority_revision is not None and authority_revision != _authority_revision(
            target[1], ceiling, locked
        ):
            raise OctopError(
                ErrorCode.EXPERT_ASSIGNMENT_CONFLICT, "assignment authority changed", status=409
            )
        for key, value in expected.items():
            row = locked.get(key)
            if (row.value if row is not None and row.enabled else None) != value:
                raise OctopError(
                    ErrorCode.EXPERT_ASSIGNMENT_CONFLICT,
                    "installation reservation changed",
                    status=409,
                )
        if any(services.agent_repo.exists_in_transaction(conn, agent_id) for agent_id in absent):
            raise OctopError(
                ErrorCode.EXPERT_ASSIGNMENT_CONFLICT, "installation reservation changed", status=409
            )
        # Confirming new installs into the assignment re-checks capacity; a discard only
        # releases capacity and must not fail on a limit lowered in the meantime.
        if POLICY_ASSIGNED_EXPERTS in updates and any(
            key.startswith(INSTALLING_EXPERT_PREFIX) and value is None
            for key, value in updates.items()
        ):
            limit = max_agents_of(locked.get(POLICY_MAX_AGENTS))
            raised_limit = updates.get(POLICY_MAX_AGENTS)
            if raised_limit is not None:
                limit = int(raised_limit)
            if (
                limit is not None
                and services.agent_repo.count_experts_in_transaction(conn, user_id) > limit
            ):
                raise OctopError(ErrorCode.AGENT_QUOTA_EXCEEDED, "capacity changed", status=409)
        services.user_policy_repo.merge_in_transaction(conn, user_id, updates)
        services.audit_repo.write_in_transaction(
            conn,
            actor=control.label if control is not None else actor[0] if actor else None,
            action=action,
            target=str(user_id),
            payload=json.dumps(updates),
        )


class ExpertAssignments:
    def __init__(self) -> None:
        self._locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()

    async def apply(
        self,
        server: Any,
        user_id: int,
        templates: list[str],
        revision: str,
        raise_limit_to: int | None,
        actor_id: int | ControlActor,
    ) -> AssignmentPlan:
        from octop.infra.agents.experts.default_agent import install_role_published_expert

        services = server.services
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:

            async def read() -> AssignmentPlan:
                return await asyncio.to_thread(plan_expert_assignment, services, user_id, templates)

            plan = await read()
            if not isinstance(actor_id, ControlActor):
                actor = await asyncio.to_thread(services.user_repo.get, actor_id)
                if actor is None or actor.disabled or actor.role != "admin":
                    raise OctopError(ErrorCode.FORBIDDEN, "assignment authority changed")
            desired = json.dumps(plan.templates, ensure_ascii=False)
            policies = services.user_policy_repo
            existing = await asyncio.to_thread(policies.get, user_id, POLICY_ASSIGNED_EXPERTS)
            if (
                not plan.conflicts
                and not plan.ready
                and not plan.install
                and existing is not None
                and existing.enabled
                and existing.value == desired
                and (raise_limit_to is None or raise_limit_to == plan.current_limit)
            ):
                return plan
            if revision != plan.revision or plan.conflicts:
                raise OctopError(
                    ErrorCode.EXPERT_ASSIGNMENT_CONFLICT,
                    "assignment preview changed",
                    status=409,
                    details={"conflicts": plan.conflicts},
                )
            if raise_limit_to is not None and (
                plan.current_limit is None
                or raise_limit_to < max(plan.current_limit, plan.required_capacity)
            ):
                raise OctopError(
                    ErrorCode.AGENT_QUOTA_EXCEEDED, "invalid capacity increase", status=409
                )
            if plan.needs_capacity_increase and raise_limit_to is None:
                raise OctopError(
                    ErrorCode.AGENT_QUOTA_EXCEEDED,
                    "explicit capacity increase required",
                    status=409,
                    details={"required": plan.required_capacity},
                )
            capacity_update = (
                {POLICY_MAX_AGENTS: str(raise_limit_to)} if raise_limit_to is not None else {}
            )
            for template in plan.install:
                pending_key = INSTALLING_EXPERT_PREFIX + template
                # A reservation whose instance row is missing (crash before insert, or after
                # a discard deleted it) is replaced with a fresh ID, never reused: a stalled
                # installer or discard still holding the old ID must not reach the new one.
                previous = plan.pending.get(template)
                reserved = uuid4().hex
                with tempfile.TemporaryDirectory(prefix="octop-expert-assignment-") as temp:
                    prepared = await asyncio.to_thread(
                        _stage,
                        services,
                        template,
                        Path(temp) / "snapshot",
                        plan.snapshot_versions[template],
                    )
                    # A crash after row insertion must never turn a partially seeded workspace into a grant.
                    await asyncio.to_thread(
                        _record_change,
                        services,
                        actor_id,
                        user_id,
                        {pending_key: reserved},
                        "user.install_assigned_expert",
                        plan.authority_revision,
                        expect={pending_key: previous.value if previous is not None else None},
                        absent=(previous.agent_id,) if previous is not None else (),
                    )
                    created = await install_role_published_expert(
                        server,
                        user_id=user_id,
                        expert_id=template,
                        agent_id=reserved,
                        prepared=prepared,
                        assignment_limit=raise_limit_to,
                        reservation=(pending_key, reserved),
                    )
                    if not created or created.get("agent_id") != reserved:
                        raise OctopError(
                            ErrorCode.EXPERT_ASSIGNMENT_CONFLICT,
                            "installation receipt missing",
                            status=409,
                        )
                    server.app_runtime.agent_registry.get_agent(reserved)
                    await asyncio.to_thread(
                        _record_change,
                        services,
                        actor_id,
                        user_id,
                        {pending_key: READY_PREFIX + reserved},
                        "user.expert_install_verified",
                        # READY stays non-invocable until the final commit re-checks authority.
                        # Comparing here would strand a verified install after a concurrent edit.
                        None,
                        expect={pending_key: reserved},
                    )
            observed = await read()
            if observed.install or observed.conflicts:
                raise OctopError(
                    ErrorCode.EXPERT_ASSIGNMENT_CONFLICT,
                    "assignment installation incomplete",
                    status=409,
                    details={"missing": observed.install, "conflicts": observed.conflicts},
                )
            confirmed = {
                INSTALLING_EXPERT_PREFIX + template: observed.pending[template].value
                for template in observed.ready
            }
            await asyncio.to_thread(
                _record_change,
                services,
                actor_id,
                user_id,
                {
                    **dict.fromkeys(confirmed),
                    **capacity_update,
                    POLICY_ASSIGNED_EXPERTS: desired,
                },
                "user.assign_experts",
                plan.authority_revision,
                expect=confirmed,
            )
            observed = await read()
            effective = await asyncio.to_thread(account_expert_grants, services.repos, user_id)
            if observed.install or observed.conflicts or effective != frozenset(plan.templates):
                raise OctopError(
                    ErrorCode.EXPERT_ASSIGNMENT_CONFLICT, "assignment readback mismatch", status=409
                )
            return observed

    async def discard(
        self, server: Any, user_id: int, template: str, agent_id: str, actor_id: int
    ) -> DiscardReceipt:
        """Remove an instance installed for ``template`` that was never confirmed.

        It must still be reserved under ``agent_id``. An installation that has not reported
        back is only discardable once its lease expires. The key moves to ``discarding:``
        first, so a concurrent final commit cannot confirm the instance being deleted, and an
        installer that has not inserted yet can no longer insert (its insert checks the key).
        The instance is deleted before the key is cleared, so a crash leaves the key pointing
        at nothing; the next apply replaces it with a fresh ID, or this call can finish.
        """
        services = server.services
        key = INSTALLING_EXPERT_PREFIX + template
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            actor = await asyncio.to_thread(services.user_repo.get, actor_id)
            if actor is None or actor.disabled or actor.role != "admin":
                raise OctopError(ErrorCode.FORBIDDEN, "assignment authority changed")
            if await asyncio.to_thread(services.user_repo.get, user_id) is None:
                raise OctopError(ErrorCode.NOT_FOUND, "user not found")
            row = await asyncio.to_thread(services.user_policy_repo.get, user_id, key)
            current = row.value if row is not None and row.enabled else None
            state, reserved = parse_reservation(current) if current else (None, None)
            if row is None or reserved != agent_id:
                raise OctopError(
                    ErrorCode.EXPERT_ASSIGNMENT_CONFLICT,
                    "installation reservation changed",
                    status=409,
                )
            discardable_at = row.updated_at + INSTALL_LEASE_SECONDS
            if state == "installing" and time.time() < discardable_at:
                raise OctopError(
                    ErrorCode.EXPERT_ASSIGNMENT_CONFLICT,
                    "installation may still be running",
                    status=409,
                    details={"discardable_at": discardable_at},
                )

            def reserved_instance(instance: Any) -> Any:
                if instance is not None and (
                    instance.user_id != user_id or instance.published_expert_id != template
                ):
                    raise OctopError(
                        ErrorCode.EXPERT_ASSIGNMENT_CONFLICT,
                        "reserved instance does not match",
                        status=409,
                    )
                return instance

            reserved_instance(await asyncio.to_thread(services.agent_repo.get, agent_id))
            marked = DISCARDING_PREFIX + agent_id
            if current != marked:
                await asyncio.to_thread(
                    _record_change,
                    services,
                    actor_id,
                    user_id,
                    {key: marked},
                    "user.expert_install_discarding",
                    None,
                    expect={key: current},
                )
            # Read again under the mark: an installer that inserted before it is seen here.
            instance = reserved_instance(await asyncio.to_thread(services.agent_repo.get, agent_id))
            deleted = False
            if instance is not None:
                try:
                    await server.app_runtime.agent_registry.delete(agent_id)
                    deleted = True
                except OctopError as exc:
                    if exc.code != ErrorCode.AGENT_NOT_FOUND:
                        raise
            await asyncio.to_thread(
                _record_change,
                services,
                actor_id,
                user_id,
                {key: None},
                "user.discard_unconfirmed_expert",
                None,
                expect={key: marked},
            )
            left = await asyncio.to_thread(services.agent_repo.get, agent_id)
            cleared = await asyncio.to_thread(services.user_policy_repo.get, user_id, key)
            if left is not None or (cleared is not None and cleared.enabled):
                raise OctopError(
                    ErrorCode.EXPERT_ASSIGNMENT_CONFLICT, "discard readback mismatch", status=409
                )
            return DiscardReceipt(template, agent_id, deleted)
