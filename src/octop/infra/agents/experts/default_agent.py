"""Bootstrap a first default agent (general-assistant) for a user.

Used by the setup wizard (pinned ``agent_id=main``) and by invite redeem
(auto-allocated agent id) so every new account starts with the same expert.
A role template can also name a published expert its accounts get.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from octop.infra.agents.experts.catalog import ExpertCatalog, build_create_spec_from_expert
from octop.infra.agents.experts.published_creation import (
    PublishedExpertInstallOptions,
    install_published_expert,
    require_published_expert,
    snapshot_welcome_message,
)
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.resource_policy import (
    POLICY_WORKSPACE_ROOT_DIR,
    effective_workspace_root_dir,
    role_default_knowledge_bases,
    role_default_published_expert,
)
from octop.infra.utils.host_dirs import host_fs_tree_root
from octop.infra.utils.locale import normalize_locale

logger = logging.getLogger(__name__)

DEFAULT_EXPERT_ID = "general-assistant"
SETUP_DEFAULT_AGENT_ID = "main"


def default_home_local_backend(*, root_dir: str | None = None) -> dict[str, Any]:
    """Same local backend as the dashboard create-from-expert default.

    Defaults to host filesystem root; pass *root_dir* when a user
    ``workspace_root_dir`` policy applies.
    """
    resolved = (root_dir or "").strip() or host_fs_tree_root()
    return {
        "type": "local_shell",
        "root_dir": resolved,
        "virtual_mode": True,
    }


def user_policy_workspace_root(server: Any, user_id: int) -> str | None:
    """Effective ``workspace_root_dir`` policy for *user_id*, or ``None``."""
    return effective_workspace_root_dir(
        server.services.user_policy_repo.get(user_id, POLICY_WORKSPACE_ROOT_DIR)
    )


def role_knowledge_base_ids(server: Any, user_id: int) -> list[str] | None:
    """Knowledge bases the user's role puts on a first expert, when still visible."""
    user = server.services.user_repo.get(user_id)
    if user is None:
        return None
    wanted = role_default_knowledge_bases(server.services.db, str(user.role))
    if not wanted:
        return None
    visible = {base.id for base in server.services.knowledge_repo.list_visible(user_id)}
    return [kb_id for kb_id in wanted if kb_id in visible] or None


async def bootstrap_default_agent(
    registry: Any,
    catalog: ExpertCatalog | None,
    *,
    user_id: int,
    locale: str = "zh",
    agent_id: str | None = None,
    root_dir: str | None = None,
    knowledge_base_ids: list[str] | None = None,
) -> Any | None:
    """Create ``general-assistant`` for *user_id* when they have no agents yet.

    Returns the created agent row, or ``None`` when skipped (user already has
    agents, or ``main`` already exists when *agent_id* is ``main``).
    """
    if agent_id == SETUP_DEFAULT_AGENT_ID and registry.get_row(SETUP_DEFAULT_AGENT_ID) is not None:
        return None
    if registry.list_agents(user_id):
        return None
    if catalog is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "expert catalog not available")
    expert = catalog.get(DEFAULT_EXPERT_ID)
    if expert is None:
        raise OctopError(
            ErrorCode.INTERNAL_ERROR,
            f"{DEFAULT_EXPERT_ID} expert template missing",
        )
    loc = normalize_locale(locale)
    spec = build_create_spec_from_expert(
        expert_id=DEFAULT_EXPERT_ID,
        expert=expert,
        user_id=user_id,
        agent_id=agent_id,
        locale=loc,
        config_extra={"backend": default_home_local_backend(root_dir=root_dir)},
        knowledge_base_ids=knowledge_base_ids,
    )
    return await registry.create(spec, defer_bootstrap=True)


async def try_bootstrap_default_agent(
    server: Any,
    *,
    user_id: int,
    locale: str = "zh",
    agent_id: str | None = None,
) -> None:
    """Best-effort wrapper used by HTTP adapters (setup / invite)."""
    if server.app_runtime is None:
        logger.warning("skip default agent bootstrap: app_runtime not ready")
        return
    try:
        await bootstrap_default_agent(
            server.app_runtime.agent_registry,
            server.expert_catalog,
            user_id=user_id,
            locale=locale,
            agent_id=agent_id,
            root_dir=user_policy_workspace_root(server, user_id),
            knowledge_base_ids=role_knowledge_base_ids(server, user_id),
        )
    except Exception as exc:  # pragma: no cover - logged; user creation should still succeed
        logger.warning("could not auto-create default agent for user %s: %s", user_id, exc)


async def install_role_published_expert(server: Any, *, user_id: int) -> Any | None:
    """Give *user_id* the published expert their role template names, once.

    Installs it the way the account would from the Experts page with the
    defaults left alone. Returns ``None`` when the role names no template or
    the account already has an expert made from it.
    """
    user = server.user_manager.get_by_id(user_id)
    if user is None:
        return None
    expert_id = role_default_published_expert(server.services.db, str(user.role))
    if expert_id is None:
        return None
    registry = server.app_runtime.agent_registry
    if any(row.published_expert_id == expert_id for row in registry.list_agents(user_id)):
        return None
    template = require_published_expert(server.services, expert_id)
    welcome_zh, welcome_en = await asyncio.to_thread(
        snapshot_welcome_message,
        Path(server.services.paths.published_experts_dir) / template.id,
    )
    welcome = (welcome_zh or welcome_en) if normalize_locale(user.locale) == "zh" else welcome_en
    return await install_published_expert(
        services=server.services,
        registry=registry,
        user=user,
        expert_id=expert_id,
        options=PublishedExpertInstallOptions(
            name=template.name,
            backend=default_home_local_backend(
                root_dir=user_policy_workspace_root(server, user_id)
            ),
            knowledge_base_ids=role_knowledge_base_ids(server, user_id),
            welcome_message=welcome or None,
        ),
    )


async def try_install_role_published_expert(server: Any, *, user_id: int) -> bool:
    """Best-effort wrapper used by HTTP adapters. True when an expert was created."""
    if server.app_runtime is None:
        logger.warning("skip role default expert: app_runtime not ready")
        return False
    try:
        return await install_role_published_expert(server, user_id=user_id) is not None
    except Exception as exc:  # logged; the account or role change should still succeed
        logger.warning("could not install the role's default expert for user %s: %s", user_id, exc)
        return False


async def backfill_role_published_expert(server: Any, role_id: str) -> int:
    """Install the role's default published expert for the accounts already in it.

    Returns how many accounts got one.
    """
    installed = 0
    for row in server.services.user_repo.list(include_disabled=True):
        if str(row.role) != role_id:
            continue
        if await try_install_role_published_expert(server, user_id=int(row.id)):
            installed += 1
    return installed
