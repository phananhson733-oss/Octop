"""HTTP adapters for the SEO workbench's Google sign-in and Search Console data."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from octop.api.common.public_base import resolve_public_base
from octop.api.deps import current_user, get_server, require_admin
from octop.api.routers.seo import seo_service
from octop.infra.seo.google.service import SeoGoogleService
from octop.infra.seo.models import GoogleConnect, GscBind, GscInspect, GscReportQuery, GscTableQuery
from octop.infra.seo.service import SeoService
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

router = APIRouter(dependencies=[Depends(require_admin())])
# Google's redirect after consent carries no JWT; the state it brings back names the admin.
callback_router = APIRouter()


def google_service(service: SeoService = Depends(seo_service)) -> SeoGoogleService:
    return SeoGoogleService(service)


class GoogleAccount(BaseModel):
    grant_id: str
    email: str | None
    search_console: bool = Field(description="The admin granted read-only Search Console access.")
    analytics: bool = Field(description="The admin granted read-only Analytics access.")
    updated_at: int


class GoogleStatus(BaseModel):
    configured: bool = Field(
        description="OCTOP_SEO_GOOGLE_CLIENT_ID and OCTOP_SEO_GOOGLE_CLIENT_SECRET are set."
    )
    redirect_uri: str = Field(
        description="The callback to register on the Google OAuth client, exactly as shown."
    )
    accounts: list[GoogleAccount]


class GoogleConnectStart(BaseModel):
    authorize_url: str


class GscSite(BaseModel):
    site_url: str
    permission_level: str | None


class GscAccountSites(GoogleAccount):
    sites: list[GscSite]
    requires_reconnect: bool
    api_disabled: bool = Field(
        description="The Search Console API is off in the OAuth client's Cloud project."
    )


class GscSites(BaseModel):
    accounts: list[GscAccountSites]


class GscConnection(BaseModel):
    project_id: str
    grant_id: str
    site_url: str
    permission_level: str
    connected_at: int
    email: str | None


class GscConnectionState(BaseModel):
    connection: GscConnection | None


class GscTotals(BaseModel):
    clicks: float
    impressions: float
    ctr: float = Field(description="0-1 fraction.")
    position: float = Field(description="Impression-weighted average; 0 when no impressions.")


class GscRange(BaseModel):
    start_date: str
    end_date: str
    prev_start_date: str
    prev_end_date: str


class GscDay(BaseModel):
    date: str
    clicks: float | None
    impressions: float | None


class GscStrikingRow(BaseModel):
    query: str
    page: str
    clicks: float
    impressions: float
    position: float


class GscDimensionRow(BaseModel):
    key: str
    clicks: float
    impressions: float
    ctr: float
    position: float | None


class GscReport(BaseModel):
    site_url: str
    email: str | None
    range: GscRange
    totals: GscTotals
    prev_totals: GscTotals
    daily: list[GscDay]
    striking_distance: list[GscStrikingRow] = Field(
        description="Queries whose best page ranks 5-20, by impressions."
    )
    countries: list[GscDimensionRow]


class GscTable(BaseModel):
    dimension: Literal["query", "page"]
    page: int
    page_size: int
    has_next_page: bool
    total_count: int | None = Field(description="Known only on the last page.")
    rows: list[GscDimensionRow]


class GscInspection(BaseModel):
    url: str
    result: dict[str, Any] | None = Field(description="Google's URL Inspection inspectionResult.")
    error: str | None = None


class GscInspections(BaseModel):
    site_url: str
    results: list[GscInspection]


@router.get("/google", response_model=GoogleStatus, summary="Google sign-in status")
async def google_status(
    request: Request,
    user: User = Depends(current_user),
    service: SeoGoogleService = Depends(google_service),
) -> Any:
    return await service.status(user.id, resolve_public_base(request))


@router.post(
    "/google/connect",
    response_model=GoogleConnectStart,
    summary="Start connecting a Google account",
    description="Returns Google's consent URL for read-only Search Console and Analytics "
    "access. The browser opens it; Google sends the admin back through the callback.",
)
async def google_connect(
    request: Request,
    body: GoogleConnect,
    user: User = Depends(current_user),
    service: SeoGoogleService = Depends(google_service),
) -> Any:
    return await service.start(user.id, resolve_public_base(request), body.return_path)


@router.delete(
    "/google/accounts/{grant_id}",
    status_code=204,
    summary="Disconnect a Google account",
    description="Projects bound to a property through it lose their Search Console binding.",
)
async def google_disconnect(
    grant_id: str,
    user: User = Depends(current_user),
    service: SeoGoogleService = Depends(google_service),
) -> Response:
    await service.delete_account(user.id, grant_id)
    return Response(status_code=204)


@callback_router.get(
    "/google/callback",
    summary="Google consent callback",
    description="No JWT: the single-use state names the admin. Redirects back to the page "
    "that started the connection with seo_google=connected or seo_google_error=<reason>.",
)
async def google_callback(
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    server: OctopServer = Depends(get_server),
) -> RedirectResponse:
    assert server.services is not None
    service = SeoGoogleService(SeoService(server.services.repos))
    location = await service.finish(state=state, code=code, error=error)
    return RedirectResponse(location, status_code=303)


@router.get(
    "/projects/{project_id}/gsc",
    response_model=GscConnectionState,
    summary="Read the project's Search Console property",
)
async def gsc_connection(
    project_id: str,
    user: User = Depends(current_user),
    service: SeoGoogleService = Depends(google_service),
) -> Any:
    return {"connection": await service.gsc_connection(user.id, project_id)}


@router.get(
    "/projects/{project_id}/gsc/sites",
    response_model=GscSites,
    summary="List Search Console properties of the connected Google accounts",
)
async def gsc_sites(
    project_id: str,
    user: User = Depends(current_user),
    service: SeoGoogleService = Depends(google_service),
) -> Any:
    return await service.gsc_sites(user.id, project_id)


@router.put(
    "/projects/{project_id}/gsc",
    response_model=GscConnection,
    summary="Bind a Search Console property to the project",
    description="The property must be on the chosen Google account with verified access.",
)
async def gsc_bind(
    project_id: str,
    body: GscBind,
    user: User = Depends(current_user),
    service: SeoGoogleService = Depends(google_service),
) -> Any:
    return await service.gsc_bind(user.id, project_id, **body.model_dump())


@router.delete(
    "/projects/{project_id}/gsc", status_code=204, summary="Unbind the Search Console property"
)
async def gsc_unbind(
    project_id: str,
    user: User = Depends(current_user),
    service: SeoGoogleService = Depends(google_service),
) -> Response:
    await service.gsc_unbind(user.id, project_id)
    return Response(status_code=204)


@router.post(
    "/projects/{project_id}/gsc/report",
    response_model=GscReport,
    summary="Search performance overview",
    description="Totals against the previous period, the daily series, striking-distance "
    "queries and countries, read live from Search Console. Free; nothing is stored.",
)
async def gsc_report(
    project_id: str,
    body: GscReportQuery,
    user: User = Depends(current_user),
    service: SeoGoogleService = Depends(google_service),
) -> Any:
    return await service.gsc_report(user.id, project_id, **body.model_dump())


@router.post(
    "/projects/{project_id}/gsc/table",
    response_model=GscTable,
    summary="One page of top queries or pages",
)
async def gsc_table(
    project_id: str,
    body: GscTableQuery,
    user: User = Depends(current_user),
    service: SeoGoogleService = Depends(google_service),
) -> Any:
    return await service.gsc_table(user.id, project_id, **body.model_dump())


@router.post(
    "/projects/{project_id}/gsc/inspect",
    response_model=GscInspections,
    summary="Inspect URLs in Search Console",
    description="Google's stored index status for 1-10 URLs of the property; not a live "
    "crawl. A failure for one URL is reported next to it.",
)
async def gsc_inspect(
    project_id: str,
    body: GscInspect,
    user: User = Depends(current_user),
    service: SeoGoogleService = Depends(google_service),
) -> Any:
    return await service.gsc_inspect(user.id, project_id, **body.model_dump())
