"""Per-user named policies: workspace root, token quota, and future rows."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.preferences import SIDEBAR_NAV_KEYS
from octop.infra.utils.host_dirs import (
    assert_backend_root_dirs_allowed,
    assert_safe_host_path,
    host_path_text,
    iter_local_backend_root_dirs,
    running_in_container,
)

POLICY_WORKSPACE_ROOT_DIR = "workspace_root_dir"
POLICY_TOKEN_QUOTA = "token_quota"
# USD per calendar month, priced from each model's input/output cost per million tokens.
POLICY_MONTHLY_BUDGET_USD = "monthly_budget_usd"
POLICY_MAX_AGENTS = "max_agents"
# Read from the role template on each request. A per-user copy is not consulted.
POLICY_VISIBLE_NAV = "visible_nav"
# Read from the role template when an account's first expert is created.
POLICY_DEFAULT_KNOWLEDGE_BASES = "default_knowledge_bases"
# Read from the role template on each request, like ``visible_nav``.
POLICY_HIDDEN_EXPERT_TABS = "hidden_expert_tabs"
# Read from the role template when an account joins the role.
POLICY_DEFAULT_PUBLISHED_EXPERT = "default_published_expert"
# Read from the role template when an account is created. "off" skips the general assistant.
POLICY_DEFAULT_ASSISTANT = "default_assistant"
# Read from the role template when an expert is installed for an account. A ``provider/model`` ref.
POLICY_DEFAULT_MODEL = "default_model"
# Tabs of the Experts page a role can hide. "My experts" always stays.
EXPERT_TAB_KEYS = frozenset({"teams", "library", "market"})


def active_policy_value(row: Any) -> str | None:
    """Return ``value`` when the policy row exists and is enabled."""
    if row is None:
        return None
    if isinstance(row, str):
        return row.strip() or None
    enabled = True
    value: Any = row
    if isinstance(row, Mapping):
        if "value" in row or "enabled" in row:
            enabled = bool(row.get("enabled", True))
            value = row.get("value")
        else:
            return None
    else:
        enabled = bool(getattr(row, "enabled", True))
        value = getattr(row, "value", None)
    if not enabled:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def workspace_root_dir_of(raw: Any) -> str | None:
    if isinstance(raw, Mapping) and "value" not in raw:
        raw = raw.get(POLICY_WORKSPACE_ROOT_DIR)
        if isinstance(raw, str):
            return raw.strip() or None
        if raw is None:
            return None
    return active_policy_value(raw)


def effective_workspace_root_dir(raw: Any) -> str | None:
    """Stored workspace-root policy, ignored when Octop runs in a container."""
    if running_in_container():
        return None
    return workspace_root_dir_of(raw)


def token_quota_of(raw: Any) -> int | None:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, Mapping) and "value" not in raw:
        raw = raw.get(POLICY_TOKEN_QUOTA)
    text = active_policy_value(raw)
    if text is None:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def monthly_budget_usd_of(raw: Any) -> float | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    if isinstance(raw, Mapping) and "value" not in raw:
        raw = raw.get(POLICY_MONTHLY_BUDGET_USD)
    text = active_policy_value(raw)
    if text is None:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def max_agents_of(raw: Any) -> int | None:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, Mapping) and "value" not in raw:
        raw = raw.get(POLICY_MAX_AGENTS)
    text = active_policy_value(raw)
    if text is None:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def visible_nav_of(raw: Any) -> list[str] | None:
    """Sidebar nav keys a role is limited to, or ``None`` when unrestricted."""
    text = active_policy_value(raw)
    if text is None:
        return None
    keys = [key for key in (part.strip() for part in text.split(",")) if key in SIDEBAR_NAV_KEYS]
    return keys or None


def role_visible_nav(db: Any, user: Any) -> list[str] | None:
    """Dashboard pages the user's role template limits them to.

    ``None`` means every page the permissions allow. This hides pages in the
    dashboard only; API access still follows permissions. Admin is never limited.
    """
    if user.is_admin:
        return None
    from octop.infra.db.repos.user_roles import UserRoleRepo

    role = UserRoleRepo(db).get(str(user.role))
    if role is None:
        return None
    return visible_nav_of(role.policy_value(POLICY_VISIBLE_NAV))


def role_default_assistant_off(db: Any, user: Any) -> bool:
    """Whether the user's role template turns off the general assistant for new accounts."""
    if user.is_admin:
        return False
    from octop.infra.db.repos.user_roles import UserRoleRepo

    role = UserRoleRepo(db).get(str(user.role))
    if role is None:
        return False
    return active_policy_value(role.policy_value(POLICY_DEFAULT_ASSISTANT)) == "off"


def hidden_expert_tabs_of(raw: Any) -> list[str]:
    """Experts-page tabs a role hides from its accounts."""
    text = active_policy_value(raw)
    if text is None:
        return []
    keys = dict.fromkeys(part.strip() for part in text.split(","))
    return [key for key in keys if key in EXPERT_TAB_KEYS]


def role_hidden_expert_tabs(db: Any, user: Any) -> list[str]:
    """Experts-page tabs the user's role template hides.

    This hides the tabs in the dashboard only; the experts, teams and market
    APIs still follow permissions. Admin is never limited.
    """
    if user.is_admin:
        return []
    from octop.infra.db.repos.user_roles import UserRoleRepo

    role = UserRoleRepo(db).get(str(user.role))
    if role is None:
        return []
    return hidden_expert_tabs_of(role.policy_value(POLICY_HIDDEN_EXPERT_TABS))


def default_knowledge_bases_of(raw: Any) -> list[str]:
    """Knowledge-base ids a role puts on its accounts' first expert."""
    text = active_policy_value(raw)
    if text is None:
        return []
    return list(dict.fromkeys(part.strip() for part in text.split(",") if part.strip()))


def role_default_knowledge_bases(db: Any, role_id: str) -> list[str]:
    """Knowledge bases the role template *role_id* puts on a first expert.

    The ids are as stored; the caller keeps the ones the account can see.
    """
    from octop.infra.db.repos.user_roles import UserRoleRepo

    role = UserRoleRepo(db).get(role_id)
    if role is None or role.is_admin:
        return []
    return default_knowledge_bases_of(role.policy_value(POLICY_DEFAULT_KNOWLEDGE_BASES))


def default_published_experts_of(raw: Any) -> list[str]:
    """Published-expert ids a role installs for its accounts, in the order chosen."""
    text = active_policy_value(raw)
    if text is None:
        return []
    return list(dict.fromkeys(part.strip() for part in text.split(",") if part.strip()))


def role_default_published_experts(db: Any, role_id: str) -> list[str]:
    """Published experts the role template *role_id* installs for its accounts.

    The ids are as stored; the caller skips a template that is gone. The first
    one is the expert the dashboard opens chat on.
    """
    from octop.infra.db.repos.user_roles import UserRoleRepo

    role = UserRoleRepo(db).get(role_id)
    if role is None or role.is_admin:
        return []
    return default_published_experts_of(role.policy_value(POLICY_DEFAULT_PUBLISHED_EXPERT))


def role_default_model(db: Any, role_id: str) -> str | None:
    """Model ref the role template *role_id* pins on the experts it installs, or ``None``."""
    from octop.infra.db.repos.user_roles import UserRoleRepo

    role = UserRoleRepo(db).get(role_id)
    if role is None or role.is_admin:
        return None
    return active_policy_value(role.policy_value(POLICY_DEFAULT_MODEL))


def public_policy_fields(rows: Sequence[Any] | Mapping[str, str] | None) -> dict[str, Any]:
    """HTTP-facing subset of known policies (disabled / missing = unlimited)."""
    by_name: dict[str, str] = {}
    if isinstance(rows, Mapping):
        by_name = {str(key): str(value) for key, value in rows.items() if value}
    elif rows:
        for row in rows:
            name = getattr(row, "name", None)
            value = active_policy_value(row)
            if isinstance(name, str) and value is not None:
                by_name[name] = value
    return {
        "workspace_root_dir": workspace_root_dir_of(by_name),
        "token_quota": token_quota_of(by_name.get(POLICY_TOKEN_QUOTA)),
        "monthly_budget_usd": monthly_budget_usd_of(by_name.get(POLICY_MONTHLY_BUDGET_USD)),
        "max_agents": max_agents_of(by_name.get(POLICY_MAX_AGENTS)),
    }


def normalize_workspace_root_dir(raw: str | None) -> str | None:
    """Return a canonical host path, or ``None`` when unrestricted.

    Setting a non-empty root is refused in container deployments — the
    container filesystem is already the isolation boundary.
    """
    if raw is None or not str(raw).strip():
        return None
    if running_in_container():
        raise OctopError(
            ErrorCode.WORKSPACE_ROOT_CONTAINER_UNSUPPORTED,
            "workspace root policy is unavailable in container deployments",
        )
    path = assert_safe_host_path(str(raw).strip(), restrict_to_home=False)
    resolved = os.path.realpath(os.fspath(path))
    drive, _tail = os.path.splitdrive(resolved)
    # Own drive root on Windows, ``/`` on POSIX. ``startswith`` after
    # ``realpath`` is the containment barrier CodeQL requires before isdir.
    base = f"{drive}{os.sep}" if drive else os.sep
    if resolved.startswith(base):
        if not os.path.isdir(resolved):
            raise OctopError(
                ErrorCode.WORKSPACE_ROOT_RESTRICTED,
                "workspace root must be a directory",
            )
        return host_path_text(path)
    raise OctopError(
        ErrorCode.WORKSPACE_ROOT_RESTRICTED,
        "path not allowed",
    )


def normalize_token_quota(raw: int | None) -> int | None:
    if raw is None:
        return None
    quota = int(raw)
    if quota < 0:
        raise OctopError(ErrorCode.FORBIDDEN, "token quota must be >= 0", status=400)
    return quota


def normalize_monthly_budget_usd(raw: float | None) -> float | None:
    if raw is None:
        return None
    budget = float(raw)
    if not budget >= 0:
        raise OctopError(ErrorCode.FORBIDDEN, "monthly budget must be >= 0", status=400)
    return budget


def normalize_max_agents(raw: int | None) -> int | None:
    if raw is None:
        return None
    limit = int(raw)
    if limit < 0:
        raise OctopError(ErrorCode.FORBIDDEN, "max agents must be >= 0", status=400)
    return limit


def normalize_visible_nav(raw: str | None) -> str | None:
    """Return comma-joined nav keys, or ``None`` when unrestricted.

    A role may leave ``chat`` out: its accounts then land on the first page it
    keeps.
    """
    if raw is None or not raw.strip():
        return None
    keys = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = sorted(set(keys) - SIDEBAR_NAV_KEYS)
    if unknown:
        raise OctopError(ErrorCode.FORBIDDEN, f"unknown nav keys: {unknown}", status=400)
    return ",".join(dict.fromkeys(keys))


def normalize_hidden_expert_tabs(raw: str | None) -> str | None:
    """Return comma-joined Experts-page tab keys, or ``None`` when none is hidden."""
    if raw is None or not raw.strip():
        return None
    keys = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = sorted(set(keys) - EXPERT_TAB_KEYS)
    if unknown:
        raise OctopError(ErrorCode.FORBIDDEN, f"unknown expert tabs: {unknown}", status=400)
    return ",".join(dict.fromkeys(keys))


def normalize_default_knowledge_bases(raw: str | None) -> str | None:
    return ",".join(default_knowledge_bases_of(raw)) or None


def normalize_default_published_experts(raw: str | None) -> str | None:
    return ",".join(default_published_experts_of(raw)) or None


def assert_backend_within_user_root(backend: Any, allowed_root: str | None) -> None:
    """Raise ``ValueError`` when a local backend root is outside *allowed_root*."""
    if backend is None:
        return
    if not allowed_root:
        assert_backend_root_dirs_allowed(backend, restrict_to_home=False)
        return
    for root_dir in iter_local_backend_root_dirs(backend):
        assert_safe_host_path(root_dir, restrict_to_root=allowed_root)


def raise_if_backend_outside_user_root(policy_repo: Any, user_id: int, backend: Any) -> None:
    allowed = effective_workspace_root_dir(policy_repo.get(user_id, POLICY_WORKSPACE_ROOT_DIR))
    try:
        assert_backend_within_user_root(backend, allowed)
    except ValueError as exc:
        code = (
            ErrorCode.WORKSPACE_ROOT_RESTRICTED if allowed else ErrorCode.WORKSPACE_OP_UNSUPPORTED
        )
        raise OctopError(code, str(exc)) from exc


def month_start_ts(timezone: str, now: float | None = None) -> int:
    """Unix time of 00:00 on the 1st of the current month in *timezone* (UTC if unknown)."""
    try:
        tz = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    now_dt = datetime.fromtimestamp(time.time() if now is None else now, tz=tz)
    return int(now_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())


def assert_token_quota_available(
    policy_repo: Any, usage_repo: Any, user_id: int, *, timezone: str = "UTC"
) -> None:
    """Raise when the user's ``total_tokens`` this calendar month (in *timezone*) reach quota."""
    quota = token_quota_of(policy_repo.get(user_id, POLICY_TOKEN_QUOTA))
    if quota is None:
        return
    used = int(usage_repo.total_tokens_for_user(user_id, since=month_start_ts(timezone)))
    if used >= quota:
        raise OctopError(
            ErrorCode.TOKEN_QUOTA_EXCEEDED,
            "token quota exceeded",
            details={"used": used, "quota": quota},
        )


def assert_monthly_budget_available(
    policy_repo: Any,
    usage_repo: Any,
    user_id: int,
    *,
    prices: Callable[[], Mapping[str, tuple[float, float]]],
    timezone: str = "UTC",
) -> None:
    """Raise when the user's spend this calendar month (in *timezone*) reaches the budget.

    Spend is input/output tokens times each model's price per million tokens;
    models without a price count as free.
    """
    budget = monthly_budget_usd_of(policy_repo.get(user_id, POLICY_MONTHLY_BUDGET_USD))
    if budget is None:
        return
    price_by_model = prices()
    spent = 0.0
    totals = usage_repo.token_totals_by_model_for_user(user_id, since=month_start_ts(timezone))
    for model, input_tokens, output_tokens in totals:
        input_price, output_price = price_by_model.get(model, (0.0, 0.0))
        spent += (input_tokens * input_price + output_tokens * output_price) / 1_000_000
    if spent >= budget:
        raise OctopError(
            ErrorCode.MONTHLY_BUDGET_EXCEEDED,
            "monthly budget exceeded",
            details={"used": f"{spent:.2f}", "quota": f"{budget:.2f}"},
        )


def assert_agent_quota_available(
    policy_repo: Any, agent_repo: Any, user_id: int, *, assignment_limit: int | None = None
) -> None:
    """Raise when the user already owns ``max_agents`` expert agents.

    A fail-fast check before any side effect; ``create_expert_within_quota`` decides.
    """
    limit = max_agents_of(policy_repo.get(user_id, POLICY_MAX_AGENTS))
    if assignment_limit is not None:
        limit = assignment_limit
    if limit is None:
        return
    _raise_if_at_agent_quota(int(agent_repo.count_by_user(user_id, kind="expert")), limit)


def create_expert_within_quota(
    repos: Any,
    user_id: int,
    insert: Callable[[Any], object],
    *,
    assignment_limit: int | None = None,
    reservation: tuple[str, str] | None = None,
) -> None:
    """Run ``insert(conn)`` only while the user owns fewer than ``max_agents`` experts.

    The count and the insert share one transaction that first locks the user row
    (``FOR UPDATE`` on PostgreSQL; SQLite transactions are ``BEGIN IMMEDIATE``). Expert
    assignment commits take the same row first, so a create and an assignment in different
    server processes queue instead of both passing a count taken before the other's write.

    ``reservation`` is ``(policy name, value)`` an assignment install must still hold; once
    an admin has discarded or replaced it, the insert is refused instead of leaving an
    instance no reservation guards.
    """
    names = (POLICY_MAX_AGENTS,) if reservation is None else (POLICY_MAX_AGENTS, reservation[0])
    with repos.db.transaction() as conn:
        repos.user_repo.assignment_principal(conn, user_id)
        locked = repos.user_policy_repo.lock_for_assignment(conn, user_id, names)
        if reservation is not None:
            held = locked.get(reservation[0])
            if held is None or not held.enabled or held.value != reservation[1]:
                raise OctopError(
                    ErrorCode.EXPERT_ASSIGNMENT_CONFLICT,
                    "installation reservation changed",
                    status=409,
                )
        limit = max_agents_of(locked.get(POLICY_MAX_AGENTS))
        if assignment_limit is not None:
            limit = assignment_limit
        if limit is not None:
            owned = repos.agent_repo.count_experts_in_transaction(conn, user_id)
            _raise_if_at_agent_quota(owned, limit)
        insert(conn)


def _raise_if_at_agent_quota(owned: int, limit: int) -> None:
    if owned >= limit:
        raise OctopError(
            ErrorCode.AGENT_QUOTA_EXCEEDED,
            "agent quota exceeded",
            details={"used": owned, "quota": limit},
        )
