"""Signed calls from the unified entry: preview, apply and read back one account's grants.

No JWT: the caller proves itself with the instance's shared control-plane key. Without that
key configured every route answers 404.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from octop.api.deps import get_server, require_database
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.control_grants import (
    ControlGrants,
    DesiredGrants,
    GrantsPreview,
    GrantsReadback,
    GrantsReceipt,
    read_grants,
)
from octop.infra.utils.control_signature import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    verify_control_signature,
)

router = APIRouter()


async def control_caller(request: Request, server: Any = Depends(get_server)) -> str:
    """Return the verified audience, or fail without revealing whether the route exists."""
    config = server.config
    if config is None or not config.control_plane_key or not config.control_plane_instance:
        raise OctopError(ErrorCode.NOT_FOUND, "not found")
    require_database(server)
    if not verify_control_signature(
        config.control_plane_key,
        method=request.method,
        path=request.url.path,
        query=request.url.query,
        body=await request.body(),
        audience=config.control_plane_instance,
        timestamp=request.headers.get(TIMESTAMP_HEADER),
        signature=request.headers.get(SIGNATURE_HEADER),
    ):
        raise OctopError(ErrorCode.AUTH_FAILED, "control signature rejected")
    return str(config.control_plane_instance)


class GrantsSelection(BaseModel):
    role: str = Field(min_length=1, description="Role template ID the account should hold.")
    templates: list[str] = Field(
        description="Published expert template IDs in display order; no duplicates."
    )
    max_agents: Annotated[int | None, Field(ge=0)] = Field(
        description="Expert limit to set; null means unlimited."
    )

    def desired(self) -> DesiredGrants:
        return DesiredGrants(self.role.strip(), list(self.templates), self.max_agents)


class GrantsApply(GrantsSelection):
    operation_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="Stable ID for this change; every retry of it reuses the ID.",
    )
    version: int = Field(
        ge=1, description="Higher than any version this instance applied for the account."
    )


@router.post(
    "/users/{user_id}/preview",
    response_model=GrantsPreview,
    summary="Preview a role, expert list and limit for one account",
    description=(
        "Signed by the unified entry. Reports the expert plan under the requested role, the "
        "account's current grants, and whether the requested limit covers the plan."
    ),
)
async def preview_grants(
    user_id: int,
    body: GrantsSelection,
    _caller: str = Depends(control_caller),
    server: Any = Depends(get_server),
) -> GrantsPreview:
    grants: ControlGrants = server.control_grants
    return await grants.preview(server, user_id, body.desired())


@router.put(
    "/users/{user_id}/grants",
    response_model=GrantsReceipt,
    summary="Apply a role, expert list and limit for one account",
    description=(
        "Signed by the unified entry. Idempotent per `operation_id`: the same content returns "
        "the stored receipt or resumes unfinished components; other content or a version not "
        "above the applied one is refused with CONTROL_OPERATION_CONFLICT. Once applied, the "
        "instance's own admin routes can no longer change these three for the account."
    ),
)
async def apply_grants(
    user_id: int,
    body: GrantsApply,
    caller: str = Depends(control_caller),
    server: Any = Depends(get_server),
) -> GrantsReceipt:
    grants: ControlGrants = server.control_grants
    return await grants.apply(
        server, user_id, body.operation_id, body.version, body.desired(), caller
    )


@router.get(
    "/users/{user_id}/grants",
    response_model=GrantsReadback,
    summary="Read back one account's role, expert list, limit and applied record",
)
async def get_grants(
    user_id: int,
    _caller: str = Depends(control_caller),
    server: Any = Depends(get_server),
) -> GrantsReadback:
    return await asyncio.to_thread(read_grants, server.services, user_id)
