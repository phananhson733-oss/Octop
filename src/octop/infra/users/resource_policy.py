"""Per-user named policies: workspace root, token quota, and future rows."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

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
POLICY_MAX_AGENTS = "max_agents"
# Read from the role template on each request. A per-user copy is not consulted.
POLICY_VISIBLE_NAV = "visible_nav"
# Read from the role template when an account's first expert is created.
POLICY_DEFAULT_KNOWLEDGE_BASES = "default_knowledge_bases"
_HOME_NAV_KEY = "chat"


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


def normalize_max_agents(raw: int | None) -> int | None:
    if raw is None:
        return None
    limit = int(raw)
    if limit < 0:
        raise OctopError(ErrorCode.FORBIDDEN, "max agents must be >= 0", status=400)
    return limit


def normalize_visible_nav(raw: str | None) -> str | None:
    """Return comma-joined nav keys, or ``None`` when unrestricted.

    ``chat`` is always kept, since that is where login lands.
    """
    if raw is None or not raw.strip():
        return None
    keys = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = sorted(set(keys) - SIDEBAR_NAV_KEYS)
    if unknown:
        raise OctopError(ErrorCode.FORBIDDEN, f"unknown nav keys: {unknown}", status=400)
    return ",".join(dict.fromkeys([_HOME_NAV_KEY, *keys]))


def normalize_default_knowledge_bases(raw: str | None) -> str | None:
    return ",".join(default_knowledge_bases_of(raw)) or None


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


def assert_token_quota_available(policy_repo: Any, usage_repo: Any, user_id: int) -> None:
    """Raise when the user's lifetime ``total_tokens`` is at or above quota."""
    quota = token_quota_of(policy_repo.get(user_id, POLICY_TOKEN_QUOTA))
    if quota is None:
        return
    used = int(usage_repo.total_tokens_for_user(user_id))
    if used >= quota:
        raise OctopError(
            ErrorCode.TOKEN_QUOTA_EXCEEDED,
            "token quota exceeded",
            details={"used": used, "quota": quota},
        )


def assert_agent_quota_available(policy_repo: Any, agent_repo: Any, user_id: int) -> None:
    """Raise when the user already owns ``max_agents`` expert agents."""
    limit = max_agents_of(policy_repo.get(user_id, POLICY_MAX_AGENTS))
    if limit is None:
        return
    owned = int(agent_repo.count_by_user(user_id, kind="expert"))
    if owned >= limit:
        raise OctopError(
            ErrorCode.AGENT_QUOTA_EXCEEDED,
            "agent quota exceeded",
            details={"used": owned, "quota": limit},
        )
