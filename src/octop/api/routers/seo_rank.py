"""HTTP adapters for SEO rank tracking: trackers, their keywords and paid checks."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from octop.api.deps import current_user, require_admin
from octop.api.routers.seo import seo_service
from octop.infra.seo.models import (
    RankCheckCreate,
    RankKeywordsAdd,
    RankTrackerCreate,
    RankTrackerUpdate,
)
from octop.infra.seo.rank import SeoRankService
from octop.infra.seo.service import SeoService
from octop.infra.users.identity import User
from octop.infra.utils.locale import resolve_request_locale

router = APIRouter(dependencies=[Depends(require_admin())])


def rank_service(service: SeoService = Depends(seo_service)) -> SeoRankService:
    return SeoRankService(service)


class RankTrackerSummary(BaseModel):
    tracker_id: str
    project_id: str
    domain: str = Field(description="Tracked domain; its subdomains count as the same site.")
    location_code: int
    language_code: str
    devices: Literal["both", "desktop", "mobile"]
    depth: int = Field(description="Results crawled per check, 10-100 in pages of 10.")
    created_at: str
    keyword_count: int
    last_checked_at: str | None = Field(description="Newest saved answer, from any check.")


class RankTracker(RankTrackerSummary):
    estimate_usd: float = Field(
        description="The most the next check can cost. Send it as max_cost_usd to approve it."
    )


class RankRun(BaseModel):
    run_id: str
    status: Literal["running", "completed", "failed", "unknown"] = Field(
        description="unknown: interrupted (a restart); a check in flight may have been charged."
    )
    started_at: str
    completed_at: str | None
    devices: Literal["both", "desktop", "mobile"]
    depth: int
    keywords_total: int
    checks_total: int = Field(description="Keywords times devices.")
    checks_done: int = Field(description="Checks answered or failed so far.")
    checks_failed: int
    cost_usd: float | None = Field(description="Supplier cost; null when a call's is unknown.")
    cost_known_usd: float
    estimate_usd: float
    error: str | None


class RankCell(BaseModel):
    position: int | None = Field(
        description="Place among organic results; null when not found within the depth."
    )
    url: str | None
    serp_features: list[str] = Field(description="Result types the SERP showed.")
    checked_at: str | None = Field(description="Null when this device was never checked.")
    previous_position: int | None
    previous_checked_at: str | None


class RankRow(BaseModel):
    keyword_id: str
    keyword: str
    desktop: RankCell
    mobile: RankCell


class RankResults(BaseModel):
    tracker: RankTracker
    run: RankRun | None = Field(description="The newest check, finished or not.")
    compare: Literal["1d", "7d", "30d", "90d"]
    rows: list[RankRow]


class RankSnapshot(BaseModel):
    run_id: str
    keyword_id: str
    keyword: str
    device: Literal["desktop", "mobile"]
    position: int | None
    url: str | None
    serp_features: list[str]
    cost_usd: float | None
    checked_at: str


class RankKeywordsAdded(BaseModel):
    added: int = Field(description="Keywords that were not tracked yet.")


_TRACKER = "/projects/{project_id}/rank-trackers/{tracker_id}"


@router.get(
    "/projects/{project_id}/rank-trackers",
    response_model=list[RankTrackerSummary],
    summary="List rank trackers",
)
async def rank_trackers(
    project_id: str,
    user: User = Depends(current_user),
    service: SeoRankService = Depends(rank_service),
) -> Any:
    return await service.list_trackers(user.id, project_id)


@router.post(
    "/projects/{project_id}/rank-trackers",
    response_model=RankResults,
    summary="Create a rank tracker",
    description="Free. Tracks a domain (the project's own when omitted) in the project's "
    "market; at most 20 trackers per project and 200 keywords per tracker.",
)
async def create_rank_tracker(
    project_id: str,
    body: RankTrackerCreate,
    request: Request,
    user: User = Depends(current_user),
    service: SeoRankService = Depends(rank_service),
) -> Any:
    return await service.create_tracker(
        user.id, project_id, locale=resolve_request_locale(request), **body.model_dump()
    )


@router.get(
    _TRACKER,
    response_model=RankResults,
    summary="Read a rank tracker's positions",
    description="Each keyword's newest position per device against the newest one older than "
    "the compare period (the first one when none is that old). No supplier request is made.",
)
async def rank_results(
    project_id: str,
    tracker_id: str,
    compare: Literal["1d", "7d", "30d", "90d"] = Query(default="7d"),
    user: User = Depends(current_user),
    service: SeoRankService = Depends(rank_service),
) -> Any:
    return await service.results(user.id, project_id, tracker_id, compare=compare)


@router.patch(
    _TRACKER,
    response_model=RankResults,
    summary="Change a rank tracker's devices or depth",
)
async def update_rank_tracker(
    project_id: str,
    tracker_id: str,
    body: RankTrackerUpdate,
    user: User = Depends(current_user),
    service: SeoRankService = Depends(rank_service),
) -> Any:
    return await service.update_tracker(
        user.id, project_id, tracker_id, **body.model_dump(exclude_none=True)
    )


@router.delete(
    _TRACKER,
    status_code=204,
    summary="Delete a rank tracker and its history",
    description="Refused with 409 while a check is running.",
)
async def delete_rank_tracker(
    project_id: str,
    tracker_id: str,
    request: Request,
    user: User = Depends(current_user),
    service: SeoRankService = Depends(rank_service),
) -> Response:
    await service.delete_tracker(
        user.id, project_id, tracker_id, locale=resolve_request_locale(request)
    )
    return Response(status_code=204)


@router.post(
    _TRACKER + "/keywords",
    response_model=RankKeywordsAdded,
    summary="Add keywords to a rank tracker",
)
async def add_rank_keywords(
    project_id: str,
    tracker_id: str,
    body: RankKeywordsAdd,
    request: Request,
    user: User = Depends(current_user),
    service: SeoRankService = Depends(rank_service),
) -> Any:
    return await service.add_keywords(
        user.id,
        project_id,
        tracker_id,
        locale=resolve_request_locale(request),
        **body.model_dump(),
    )


@router.delete(
    _TRACKER + "/keywords/{keyword_id}",
    status_code=204,
    summary="Stop tracking a keyword",
    description="Its saved positions stay readable through its history.",
)
async def remove_rank_keyword(
    project_id: str,
    tracker_id: str,
    keyword_id: str,
    user: User = Depends(current_user),
    service: SeoRankService = Depends(rank_service),
) -> Response:
    await service.remove_keyword(user.id, project_id, tracker_id, keyword_id)
    return Response(status_code=204)


@router.get(
    _TRACKER + "/keywords/{keyword_id}/history",
    response_model=list[RankSnapshot],
    summary="Read a keyword's position history",
    description="The newest 200 saved positions, oldest first. No supplier request is made.",
)
async def rank_history(
    project_id: str,
    tracker_id: str,
    keyword_id: str,
    user: User = Depends(current_user),
    service: SeoRankService = Depends(rank_service),
) -> Any:
    return await service.history(user.id, project_id, tracker_id, keyword_id)


@router.post(
    _TRACKER + "/checks",
    response_model=RankRun,
    status_code=202,
    summary="Buy a rank check",
    description="Requires explicit paid confirmation and max_cost_usd at least the tracker's "
    "estimate_usd. Buys one live Google SERP per keyword and device, crawling only until the "
    "domain shows, in the background; poll the tracker for progress. One check at a time per "
    "tracker. No retries. Reuse the idempotency key to read an existing attempt.",
)
async def start_rank_check(
    project_id: str,
    tracker_id: str,
    body: RankCheckCreate,
    request: Request,
    user: User = Depends(current_user),
    service: SeoRankService = Depends(rank_service),
) -> Any:
    return await service.start_check(
        user.id,
        project_id,
        tracker_id,
        locale=resolve_request_locale(request),
        **body.model_dump(),
    )
