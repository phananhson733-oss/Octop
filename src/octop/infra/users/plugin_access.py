"""Role-scoped plugin UI grants, independent of tool execution grants.

Missing policy preserves existing internal access. An explicit empty JSON list
blocks all plugin UI. Malformed stored grants fail closed, never to legacy mode.
"""

from __future__ import annotations

from typing import Any

from octop.i18n import error_message
from octop.infra.db.pool import DatabasePool
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.resource_grants import normalize_resource_grants, role_resource_grants

POLICY_ALLOWED_PLUGIN_UI = "allowed_plugin_ui"
normalize_plugin_ui_grants = normalize_resource_grants


def plugin_ui_grants(db: DatabasePool, user: Any) -> frozenset[str] | None:
    return role_resource_grants(db, user, POLICY_ALLOWED_PLUGIN_UI)


def assert_plugin_ui_allowed(db: DatabasePool, user: Any, plugin_id: str) -> None:
    grants = plugin_ui_grants(db, user)
    if grants is not None and plugin_id not in grants:
        raise OctopError(ErrorCode.FORBIDDEN, error_message(ErrorCode.FORBIDDEN))
