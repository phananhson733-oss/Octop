"""HTTP adapter for curated-expert configuration authorization."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Depends

from octop.api.deps import current_user, get_server
from octop.infra.users.expert_access import assert_expert_management


async def require_expert_management(
    user: Any = Depends(current_user), server: Any = Depends(get_server)
) -> None:
    await asyncio.get_running_loop().run_in_executor(
        None, assert_expert_management, server.services.repos, user.id
    )
