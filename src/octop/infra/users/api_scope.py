"""Role policy that confines an account's API access to one narrow product surface.

``visible_nav`` only hides dashboard pages, and many routes answer any signed-in
user. A role meant for outside customers sets ``api_scope`` so the server
refuses every ``/api`` route outside that surface, whatever the permissions.
The route lists live in ``octop.api.middleware.api_scope``.

Missing policy means unrestricted. An unknown stored value fails closed, and so
does a role that is gone: built-in roles always have a row, so a missing one is a
deleted role or a mistyped id, and its accounts must not fall back to full access.
"""

from __future__ import annotations

from typing import Any

from octop.infra.errors import ErrorCode, OctopError

POLICY_API_SCOPE = "api_scope"
# Only the Workbench's plugin pages (with ``allowed_plugin_ui``) and the account itself.
API_SCOPE_WORKBENCH = "workbench"
API_SCOPES = frozenset({API_SCOPE_WORKBENCH})
# Stored value we cannot read: allow nothing beyond signing out.
API_SCOPE_NONE = "none"


def normalize_api_scope(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    scope = raw.strip()
    if scope not in API_SCOPES:
        raise OctopError(ErrorCode.FORBIDDEN, f"unknown api scope: {scope}", status=400)
    return scope


def role_api_scope(db: Any, user: Any) -> str | None:
    """The scope of the user's current role, read on every request."""
    if user.is_admin:
        return None
    from octop.infra.db.repos.user_roles import UserRoleRepo

    role = UserRoleRepo(db).get(str(user.role))
    if role is None:
        return API_SCOPE_NONE
    raw = role.policy_value(POLICY_API_SCOPE)
    if raw is None:
        return None
    return raw if raw in API_SCOPES else API_SCOPE_NONE
