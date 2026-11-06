"""Global SecurityPolicy persistence in the settings table."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from typing import Any

from octop_harness.security.models import FilesystemPolicy, FilesystemRule, SecurityPolicy

from octop.infra.db.repos.settings import SettingsRepo

logger = logging.getLogger(__name__)

_SETTINGS_KEY = "security_policy"
# Deployment-level path denies for the built-in file tools, ``os.pathsep``-separated globs.
# They go in front of the stored rules (first match wins), so an admin or agent policy
# cannot re-open them, and they keep the guard on even when the stored policy turns it off.
# Writes are confined separately (``OCTOP_FS_CONFINE_WRITES``, see ``path_rules_guard``).
_DENY_ENV = "OCTOP_FS_DENY"


def _default_policy() -> SecurityPolicy:
    """Octop defaults when no ``security_policy`` row exists in settings."""
    data = SecurityPolicy.defaults().to_dict()
    data["hitl"]["enabled"] = False
    data["tool_guard"] = {"enabled": True, "mode": "warn"}
    return SecurityPolicy.from_dict(data)


class SecuritySettingsStore:
    """Read/write global :class:`SecurityPolicy` in the settings table."""

    def __init__(self, *, settings_repo: SettingsRepo) -> None:
        self._settings = settings_repo

    def load(self) -> SecurityPolicy:
        raw = self._settings.get(_SETTINGS_KEY)
        if not raw:
            return _default_policy()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("invalid security_policy JSON in settings; using defaults")
            return _default_policy()
        if not isinstance(data, dict):
            return _default_policy()
        return SecurityPolicy.from_dict(data)

    def save(self, policy: SecurityPolicy | dict[str, Any]) -> SecurityPolicy:
        resolved = (
            policy if isinstance(policy, SecurityPolicy) else SecurityPolicy.from_dict(policy)
        )
        self._settings.set(_SETTINGS_KEY, json.dumps(resolved.to_dict(), ensure_ascii=False))
        return resolved

    def harness_policy(self) -> SecurityPolicy:
        return with_deployment_path_denies(self.load())


def tool_execution_may_pause(policy: SecurityPolicy) -> bool:
    """True when a tool call can pause for human approval.

    Covers the admin HITL switch and command-guard ``require_approval``.
    ``ask_user_question`` is a separate collaboration pause and is ignored here.
    """
    if policy.hitl.enabled:
        return True
    guard = policy.tool_guard
    return bool(guard.enabled and guard.mode == "require_approval")


def _env_globs(name: str) -> list[str]:
    return [p.strip() for p in os.environ.get(name, "").split(os.pathsep) if p.strip()]


def with_deployment_path_denies(policy: SecurityPolicy) -> SecurityPolicy:
    """Prepend the ``OCTOP_FS_DENY`` rule to *policy*."""
    deny = _env_globs(_DENY_ENV)
    if not deny:
        return policy
    rule = FilesystemRule(operations=["read", "write"], paths=deny, mode="deny")
    stored = policy.filesystem.rules if policy.filesystem.enabled else []
    return replace(policy, filesystem=FilesystemPolicy(enabled=True, rules=[rule, *stored]))
