"""Apply the unified entry's desired role, expert limit and expert list to one account.

The entry is the single write source for these three once it has applied anything here
(plan §3.2.3, §3.2.5). Each request carries an operation ID and a version:

- the same operation with the same content returns the stored receipt, or resumes the
  components that did not finish; with other content it is refused;
- a new operation must carry a higher version than the one recorded, so a delayed or replayed
  older request can never roll the account back.

Components run in order role, expert limit, experts, and stop at the first failure. The
record keeps a state per component, so a retry of the same operation only redoes what did
not finish. Expert installs reuse ``ExpertAssignments``, which reuses existing instances.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal
from weakref import WeakValueDictionary

from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.expert_access import INSTALLING_EXPERT_PREFIX, POLICY_ASSIGNED_EXPERTS
from octop.infra.users.expert_assignment import (
    POLICY_CONTROL_RECORD,
    AssignmentPlan,
    ControlActor,
    ExpertAssignments,
    _record_change,
    control_operation_of,
    plan_expert_assignment,
)
from octop.infra.users.resource_policy import (
    POLICY_MAX_AGENTS,
    POLICY_TOKEN_QUOTA,
    POLICY_WORKSPACE_ROOT_DIR,
    max_agents_of,
    normalize_max_agents,
    normalize_token_quota,
    normalize_workspace_root_dir,
)
from octop.infra.users.role_templates import (
    policy_kwargs_replace_from_pairs,
    resolve_role_template,
)

COMPONENTS = ("role", "max_agents", "experts")
ComponentState = Literal["pending", "applied", "failed"]


@dataclass(frozen=True)
class DesiredGrants:
    role: str
    templates: list[str]
    max_agents: int | None

    def digest(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class ComponentResult:
    state: ComponentState
    code: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ControlRecord:
    """What the instance last accepted from the entry for this account."""

    operation_id: str
    version: int
    digest: str
    state: Literal["applying", "applied", "partial"]
    components: dict[str, ComponentResult]
    # True when this operation changes the role, or inherits an unfinished role change from
    # the operation it superseded; the role template is then (re)applied until it succeeds.
    role_change: bool
    updated_at: int

    def encode(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)

    @classmethod
    def decode(cls, raw: str) -> ControlRecord:
        data = json.loads(raw)
        return cls(
            operation_id=str(data["operation_id"]),
            version=int(data["version"]),
            digest=str(data["digest"]),
            state=data["state"],
            components={
                name: ComponentResult(**value) for name, value in data["components"].items()
            },
            role_change=bool(data["role_change"]),
            updated_at=int(data["updated_at"]),
        )


@dataclass(frozen=True)
class GrantsReadback:
    user_id: int
    role: str
    disabled: bool
    max_agents: int | None
    # Confirmed curated expert list in display order; ``None`` when none was ever assigned.
    templates: list[str] | None
    # Installing keys still open, ``template -> reservation value``.
    pending: dict[str, str]
    record: ControlRecord | None


@dataclass(frozen=True)
class GrantsReceipt:
    operation_id: str
    version: int
    state: Literal["applied", "partial"]
    components: dict[str, ComponentResult]
    # True when this request ran nothing and returned what an earlier delivery stored.
    replayed: bool
    readback: GrantsReadback


@dataclass(frozen=True)
class GrantsPreview:
    plan: AssignmentPlan
    current: GrantsReadback
    capacity_ok: bool


def _conflict(reason: str, message: str, **details: Any) -> OctopError:
    return OctopError(
        ErrorCode.CONTROL_OPERATION_CONFLICT, message, details={"reason": reason, **details}
    )


def read_grants(services: SharedServices, user_id: int) -> GrantsReadback:
    user = services.user_repo.get(user_id)
    if user is None:
        raise OctopError(ErrorCode.NOT_FOUND, "user not found")
    rows = {row.name: row for row in services.user_policy_repo.list_for_user(user_id)}
    active = {name: row.value for name, row in rows.items() if row.enabled}
    assigned = active.get(POLICY_ASSIGNED_EXPERTS)
    record = active.get(POLICY_CONTROL_RECORD)
    return GrantsReadback(
        user_id=user_id,
        role=str(user.role),
        disabled=bool(user.disabled),
        max_agents=max_agents_of(rows.get(POLICY_MAX_AGENTS)),
        templates=json.loads(assigned) if assigned is not None else None,
        pending={
            name.removeprefix(INSTALLING_EXPERT_PREFIX): value
            for name, value in active.items()
            if name.startswith(INSTALLING_EXPERT_PREFIX)
        },
        record=ControlRecord.decode(record) if record is not None else None,
    )


def _validate(services: SharedServices, user_id: int, desired: DesiredGrants) -> None:
    """Refuse a request that can never succeed before anything is recorded."""
    user = services.user_repo.get(user_id)
    if user is None:
        raise OctopError(ErrorCode.NOT_FOUND, "user not found")
    if user.disabled or user.role == "admin":
        raise OctopError(ErrorCode.FORBIDDEN, "account unavailable for curated assignment")
    role_id, _name, _permissions, _policies = resolve_role_template(services, desired.role)
    if role_id == "admin":
        raise OctopError(ErrorCode.FORBIDDEN, "the entry cannot grant administrator", status=400)
    if len(set(desired.templates)) != len(desired.templates) or any(
        not item or item != item.strip() for item in desired.templates
    ):
        raise OctopError(ErrorCode.FORBIDDEN, "invalid template list", status=400)
    if desired.max_agents is not None and desired.max_agents < 0:
        raise OctopError(ErrorCode.FORBIDDEN, "max agents must be >= 0", status=400)


def _claim(
    services: SharedServices, user_id: int, previous: str | None, record: ControlRecord, label: str
) -> None:
    """Record a new operation as this account's owner, if no one else did since ``previous``."""
    with services.db.transaction() as conn:
        target = services.user_repo.assignment_principal(conn, user_id)
        if not target or target[2] or target[1] == "admin":
            raise OctopError(ErrorCode.FORBIDDEN, "account unavailable for curated assignment")
        locked = services.user_policy_repo.lock_for_assignment(
            conn, user_id, (POLICY_CONTROL_RECORD,)
        )
        row = locked.get(POLICY_CONTROL_RECORD)
        if (row.value if row is not None and row.enabled else None) != previous:
            raise _conflict("superseded", "control record changed")
        services.user_policy_repo.merge_in_transaction(
            conn, user_id, {POLICY_CONTROL_RECORD: record.encode()}
        )
        services.audit_repo.write_in_transaction(
            conn,
            actor=label,
            action="user.control_operation_claimed",
            target=str(user_id),
            payload=json.dumps({"operation_id": record.operation_id, "version": record.version}),
        )


def _settle(
    services: SharedServices, user_id: int, final: ControlRecord, label: str
) -> ControlRecord:
    """Store this delivery's outcome; returns the record that now stands.

    Two processes can deliver the same operation at once. An outcome never replaces one where
    the operation is already applied, so a late failure cannot undo a completed delivery.
    """
    with services.db.transaction() as conn:
        target = services.user_repo.assignment_principal(conn, user_id)
        if not target or target[2] or target[1] == "admin":
            raise OctopError(ErrorCode.FORBIDDEN, "account unavailable for curated assignment")
        locked = services.user_policy_repo.lock_for_assignment(
            conn, user_id, (POLICY_CONTROL_RECORD,)
        )
        row = locked.get(POLICY_CONTROL_RECORD)
        current = ControlRecord.decode(row.value) if row is not None and row.enabled else None
        if current is None or current.operation_id != final.operation_id:
            raise _conflict("superseded", "control operation superseded")
        if current.state == "applied":
            return current
        # Keep components another delivery of this operation already finished.
        kept = {
            name: current.components[name]
            if current.components[name].state == "applied"
            else final.components[name]
            for name in COMPONENTS
        }
        final = replace(final, components=kept)
        services.user_policy_repo.merge_in_transaction(
            conn, user_id, {POLICY_CONTROL_RECORD: final.encode()}
        )
        services.audit_repo.write_in_transaction(
            conn,
            actor=label,
            action="user.control_operation_" + final.state,
            target=str(user_id),
            payload=json.dumps(
                {
                    "operation_id": final.operation_id,
                    "version": final.version,
                    "state": final.state,
                }
            ),
        )
        return final


def _role_policy_updates(policies: list[tuple[str, str]]) -> dict[str, str | None]:
    """The role template's resource policies as a full replace, as a native role change sets.

    Keys stay in the order ``UserManager.set_resource_policy`` writes them; ``_finish_role``
    relies on that order so the two take these rows' locks the same way round.
    """
    kwargs = policy_kwargs_replace_from_pairs(policies)
    quota = normalize_token_quota(kwargs["token_quota"])
    limit = normalize_max_agents(kwargs["max_agents"])
    return {
        POLICY_WORKSPACE_ROOT_DIR: normalize_workspace_root_dir(kwargs["workspace_root_dir"]),
        POLICY_TOKEN_QUOTA: None if quota is None else str(quota),
        POLICY_MAX_AGENTS: None if limit is None else str(limit),
    }


def _finish_role(
    services: SharedServices, user_id: int, actor: ControlActor, updates: dict[str, str | None]
) -> None:
    """Write the role template's policies and mark the role component applied, atomically.

    Another process delivering the same operation finds the component done and leaves the
    policies alone, so a token quota changed after it completed is not reset.
    """
    with services.db.transaction() as conn:
        target = services.user_repo.assignment_principal(conn, user_id)
        if not target or target[2] or target[1] == "admin":
            raise OctopError(ErrorCode.FORBIDDEN, "account unavailable for curated assignment")
        # Only the record is locked up front. The policy rows are then written in the order a
        # native resource-policy write uses, which holds no user lock: locking them here by
        # name instead would take them the other way round and could deadlock with it.
        locked = services.user_policy_repo.lock_for_assignment(
            conn, user_id, (POLICY_CONTROL_RECORD,)
        )
        row = locked.get(POLICY_CONTROL_RECORD)
        current = ControlRecord.decode(row.value) if row is not None and row.enabled else None
        if current is None or current.operation_id != actor.operation_id:
            raise _conflict("superseded", "control operation superseded")
        if current.state == "applied" or current.components["role"].state == "applied":
            return
        done = replace(
            current, components={**current.components, "role": ComponentResult("applied")}
        )
        services.user_policy_repo.merge_in_transaction(
            conn, user_id, {**updates, POLICY_CONTROL_RECORD: done.encode()}
        )
        services.audit_repo.write_in_transaction(
            conn,
            actor=actor.label,
            action="user.set_resource_policy",
            target=str(user_id),
            payload=json.dumps(updates),
        )


def _same_operation(
    record: ControlRecord | None, operation_id: str, version: int, digest: str
) -> ControlRecord | None:
    """The stored record when it is this operation; other content under its ID is refused."""
    if record is None or record.operation_id != operation_id:
        return None
    if record.digest != digest or record.version != version:
        raise _conflict("operation_mismatch", "operation id reused with other content")
    return record


class ControlGrants:
    def __init__(self, assignments: ExpertAssignments) -> None:
        self._assignments = assignments
        self._locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()

    async def preview(self, server: Any, user_id: int, desired: DesiredGrants) -> GrantsPreview:
        services = server.services
        await asyncio.to_thread(_validate, services, user_id, desired)
        plan = await asyncio.to_thread(
            plan_expert_assignment,
            services,
            user_id,
            desired.templates,
            role_override=desired.role,
        )
        current = await asyncio.to_thread(read_grants, services, user_id)
        return GrantsPreview(
            plan=plan,
            current=current,
            capacity_ok=desired.max_agents is None or plan.required_capacity <= desired.max_agents,
        )

    async def apply(
        self,
        server: Any,
        user_id: int,
        operation_id: str,
        version: int,
        desired: DesiredGrants,
        caller: str,
    ) -> GrantsReceipt:
        services = server.services
        actor = ControlActor(f"control-plane:{caller}", operation_id)
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            current = await asyncio.to_thread(read_grants, services, user_id)
            previous = current.record
            digest = desired.digest()
            existing = _same_operation(previous, operation_id, version, digest)
            if existing is None:
                if previous is not None and version <= previous.version:
                    raise _conflict(
                        "stale_version",
                        "a newer version is already recorded",
                        applied_version=previous.version,
                    )
                await asyncio.to_thread(_validate, services, user_id, desired)
                inherited = previous is not None and previous.components["role"].state != "applied"
                record = ControlRecord(
                    operation_id=operation_id,
                    version=version,
                    digest=digest,
                    state="applying",
                    components={name: ComponentResult("pending") for name in COMPONENTS},
                    role_change=current.role != desired.role or inherited,
                    updated_at=int(time.time()),
                )
                raw = previous.encode() if previous is not None else None
                try:
                    await asyncio.to_thread(_claim, services, user_id, raw, record, actor.label)
                except OctopError as exc:
                    if exc.code != ErrorCode.CONTROL_OPERATION_CONFLICT:
                        raise
                    # Another process may have claimed this same operation first: resume it.
                    current = await asyncio.to_thread(read_grants, services, user_id)
                    existing = _same_operation(current.record, operation_id, version, digest)
                    if existing is None:
                        raise
            if existing is not None:
                if existing.state == "applied":
                    return GrantsReceipt(
                        operation_id, version, "applied", existing.components, True, current
                    )
                record = existing

            components = dict(record.components)
            for name in COMPONENTS:
                if components[name].state == "applied":
                    continue
                try:
                    await self._run(server, user_id, name, record, desired, actor)
                    components[name] = ComponentResult("applied")
                except OctopError as exc:
                    if exc.code == ErrorCode.CONTROL_OPERATION_CONFLICT:
                        raise
                    components[name] = ComponentResult("failed", exc.code.value, exc.details)
                    break
            readback = await asyncio.to_thread(read_grants, services, user_id)
            matches = (
                readback.role == desired.role
                and readback.max_agents == desired.max_agents
                and readback.templates == desired.templates
            )
            done = all(item.state == "applied" for item in components.values()) and matches
            state: Literal["applied", "partial"] = "applied" if done else "partial"
            final = replace(
                record,
                state=state,
                components=components,
                updated_at=int(time.time()),
            )
            stored = await asyncio.to_thread(_settle, services, user_id, final, actor.label)
            readback = await asyncio.to_thread(read_grants, services, user_id)
            outcome: Literal["applied", "partial"] = (
                "applied" if stored.state == "applied" else "partial"
            )
            return GrantsReceipt(operation_id, version, outcome, stored.components, False, readback)

    async def _run(
        self,
        server: Any,
        user_id: int,
        name: str,
        record: ControlRecord,
        desired: DesiredGrants,
        actor: ControlActor,
    ) -> None:
        services = server.services
        if name == "role":
            if not record.role_change:
                return
            # The users-table writes are not atomic with this check; see "Residuals" in the
            # status doc. The policies and the component's completion are (``_finish_role``).
            owner = await asyncio.to_thread(
                services.user_policy_repo.get, user_id, POLICY_CONTROL_RECORD
            )
            stored = owner.value if owner and owner.enabled else None
            if control_operation_of(stored) != actor.operation_id:
                raise _conflict("superseded", "control operation superseded")
            assert stored is not None
            current = ControlRecord.decode(stored)
            if current.state == "applied" or current.components["role"].state == "applied":
                return  # another delivery of this operation already did it
            row = await asyncio.to_thread(services.user_repo.get, user_id)
            assert row is not None
            role_id, role_name, permissions, policies = await asyncio.to_thread(
                resolve_role_template, services, desired.role
            )
            updates = _role_policy_updates(policies)
            # Same writes as a native role change, minus the role's default expert: the
            # entry's expert list is explicit and is applied as its own component.
            await server.user_manager.set_role(row.username, role_id)
            await asyncio.to_thread(services.user_repo.set_role_name, user_id, role_name)
            await server.user_manager.set_permissions(row.username, permissions)
            await asyncio.to_thread(_finish_role, services, user_id, actor, updates)
        elif name == "max_agents":
            # Before the experts, so installs are checked against the limit being set.
            value = None if desired.max_agents is None else str(desired.max_agents)
            await asyncio.to_thread(
                _record_change,
                services,
                actor,
                user_id,
                {POLICY_MAX_AGENTS: value},
                "user.set_max_agents",
                None,
            )
        else:
            plan = await asyncio.to_thread(
                plan_expert_assignment, services, user_id, desired.templates
            )
            # Same rule the preview reports as ``capacity_ok``; reusing every expert would
            # otherwise skip the assignment's own capacity check.
            if desired.max_agents is not None and plan.required_capacity > desired.max_agents:
                raise OctopError(
                    ErrorCode.AGENT_QUOTA_EXCEEDED,
                    "expert limit below the experts this account keeps",
                    status=409,
                    details={"required": plan.required_capacity, "limit": desired.max_agents},
                )
            await self._assignments.apply(
                server, user_id, desired.templates, plan.revision, None, actor
            )
