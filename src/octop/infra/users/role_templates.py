"""Apply a role template's permissions and resource policies to one account."""

from __future__ import annotations

from typing import Any

from octop.infra.errors import ErrorCode, OctopError


def resolve_role_template(
    services: Any, role_id: str
) -> tuple[str, str, list[str], list[tuple[str, str]]]:
    """Return ``(role_id, role_name, permissions, policies)`` for a template id."""
    from octop.infra.db.repos.user_roles import UserRoleRepo
    from octop.infra.users.permissions import PERMISSIONS

    cleaned = (role_id or "").strip()
    if not cleaned:
        raise OctopError(ErrorCode.FORBIDDEN, "role is required", status=400)
    role = UserRoleRepo(services.db).get(cleaned)
    if role is None:
        raise OctopError(ErrorCode.NOT_FOUND, "role not found")
    if role.is_admin:
        return role.user_role_id, role.user_role_name, [], []
    permissions = [key for key in role.permissions if key in PERMISSIONS]
    return role.user_role_id, role.user_role_name, permissions, list(role.policies)


def policy_kwargs_replace_from_pairs(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """Full replace: every known policy is set or cleared.

    Used when applying a role template so limits from a previous role cannot
    linger after a switch (including to admin, which has no resource limits).
    """
    from octop.infra.users.resource_policy import (
        POLICY_MAX_AGENTS,
        POLICY_MONTHLY_BUDGET_USD,
        POLICY_TOKEN_QUOTA,
        POLICY_WORKSPACE_ROOT_DIR,
        max_agents_of,
        monthly_budget_usd_of,
        token_quota_of,
    )

    by_name = dict(pairs)
    return {
        "workspace_root_dir": by_name.get(POLICY_WORKSPACE_ROOT_DIR),
        "token_quota": token_quota_of(by_name.get(POLICY_TOKEN_QUOTA)),
        "max_agents": max_agents_of(by_name.get(POLICY_MAX_AGENTS)),
        "monthly_budget_usd": monthly_budget_usd_of(by_name.get(POLICY_MONTHLY_BUDGET_USD)),
    }
