"""JSON resource allowlists shared by plugin UI and expert authorization."""

from __future__ import annotations

import json
from typing import Any

from octop.i18n import error_message
from octop.infra.db.pool import DatabasePool
from octop.infra.errors import ErrorCode, OctopError


def normalize_resource_grants(raw: str) -> str:
    try:
        values = json.loads(raw)
    except (ValueError, TypeError):
        raise OctopError(
            ErrorCode.FORBIDDEN, error_message(ErrorCode.FORBIDDEN), status=400
        ) from None
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value.strip() or value != value.strip()
        for value in values
    ):
        raise OctopError(ErrorCode.FORBIDDEN, error_message(ErrorCode.FORBIDDEN), status=400)
    return json.dumps(list(dict.fromkeys(values)), ensure_ascii=False)


def role_resource_grants(db: DatabasePool, user: Any, policy: str) -> frozenset[str] | None:
    """Read current role on every request so an old JWT cannot retain grants."""
    if user.is_admin:
        return None
    from octop.infra.db.repos.user_roles import UserRoleRepo

    role = UserRoleRepo(db).get(str(user.role))
    return grants_from_role(role, policy)


def grants_from_role(role: Any, policy: str) -> frozenset[str] | None:
    if role is None:
        return frozenset()
    raw = role.policy_value(policy)
    if raw is None:
        return None
    try:
        return frozenset(json.loads(normalize_resource_grants(raw)))
    except OctopError:
        return frozenset()
