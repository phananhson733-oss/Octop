"""HTTP adapters for the SEO workbench's Google sign-in, Search Console and Analytics data."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from octop.api.common.public_base import resolve_public_base
from octop.api.deps import get_server, require_admin, require_permission
from octop.api.routers.seo import seo_owner, seo_service
from octop.infra.seo.google.service import SeoGoogleService
from octop.infra.seo.models import (
    Ga4Bind,
    Ga4Overview,
    Ga4Report,
    GoogleConnect,
    GscBind,
    GscInspect,
    GscReportQuery,
    GscTableQuery,
)
from octop.infra.seo.service import SeoService
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

router = APIRouter(dependencies=[Depends(require_permission("seo"))])
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


class Ga4Property(BaseModel):
    property_id: str = Field(description="properties/<number>")
    display_name: str
    account_display_name: str


class Ga4AccountProperties(GoogleAccount):
    properties: list[Ga4Property]
    requires_reconnect: bool
    api_disabled: bool = Field(
        description="The Analytics Admin API is off in the OAuth client's Cloud project."
    )


class Ga4Properties(BaseModel):
    accounts: list[Ga4AccountProperties]


class Ga4Connection(BaseModel):
    project_id: str
    grant_id: str
    property_id: str
    display_name: str
    time_zone: str = Field(description="The property's time zone; report days follow it.")
    currency_code: str
    connected_at: int
    email: str | None


class Ga4ConnectionState(BaseModel):
    connection: Ga4Connection | None


class Ga4DateRange(BaseModel):
    start_date: str
    end_date: str


class Ga4Source(BaseModel):
    property_id: str
    display_name: str
    email: str | None


class Ga4Comparison(BaseModel):
    current: float | None
    previous: float | None
    absolute_change: float | None
    percent_change: float | None = Field(description="0-1 fraction; null when previous is 0.")


class Ga4OverviewRequest(BaseModel):
    requested_date_range: Ga4DateRange | None
    resolved_date_range: Ga4DateRange
    previous_date_range: Ga4DateRange
    time_zone: str
    currency_code: str
    channel: str
    trend: Literal["daily", "weekly"]


class Ga4OverviewResult(BaseModel):
    source: Ga4Source
    request: Ga4OverviewRequest
    current: dict[str, Any] | None = Field(description="Organic totals by metric name.")
    previous: dict[str, Any] | None
    comparison: dict[str, Ga4Comparison]
    trend: list[dict[str, Any]] = Field(
        description="Rows keyed by date (YYYYMMDD) or yearWeek (YYYYWW) and metric names."
    )
    diagnostics: list[dict[str, Any]]
    has_limited_data: bool = Field(
        description="Thresholded, sampled, (other)-folded or restricted data."
    )
    quota: dict[str, Any] | None
    warnings: list[str] = Field(description="end_date_clamped, trend_truncated.")


class Ga4ReportRequest(BaseModel):
    requested_date_range: Ga4DateRange | None
    resolved_date_range: Ga4DateRange
    time_zone: str
    currency_code: str
    channel: str
    report_kind: str
    breakdown: str
    dimensions: list[str]
    metrics: list[str]
    limit: int
    offset: int


class Ga4PageInfo(BaseModel):
    offset: int
    limit: int
    has_more: bool
    next_offset: int | None


class Ga4ReportResult(BaseModel):
    source: Ga4Source
    request: Ga4ReportRequest
    row_count: int
    total_row_count: int
    rows: list[dict[str, Any]] = Field(description="Rows keyed by dimension and metric names.")
    page_info: Ga4PageInfo
    report_metadata: dict[str, Any]
    quota: dict[str, Any] | None
    warnings: list[str]


@router.get("/google", response_model=GoogleStatus, summary="Google sign-in status")
async def google_status(
    request: Request,
    owner: int = Depends(seo_owner),
    service: SeoGoogleService = Depends(google_service),
) -> Any:
    return await service.status(owner, resolve_public_base(request))


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
    user: User = Depends(require_admin()),
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
    user: User = Depends(require_admin()),
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
    owner: int = Depends(seo_owner),
    service: SeoGoogleService = Depends(google_service),
) -> Any:
    return {"connection": await service.gsc_connection(owner, project_id)}


@router.get(
    "/projects/{project_id}/gsc/sites",
    response_model=GscSites,
    summary="List Search Console properties of the connected Google accounts",
)
async def gsc_sites(
    project_id: str,
    owner: int = Depends(seo_owner),
    service: SeoGoogleService = Depends(google_service),
) -> Any:
    return await service.gsc_sites(owner, project_id)


@router.put(
    "/projects/{project_id}/gsc",
    response_model=GscConnection,
    summary="Bind a Search Console property to the project",
    description="The property must be on the chosen Google account with verified access.",
)
async def gsc_bind(
    project_id: str,
    body: GscBind,
    owner: int = Depends(seo_owner),
    service: SeoGoogleService = Depends(google_service),
) -> Any:
    return await service.gsc_bind(owner, project_id, **body.model_dump())


@router.delete(
    "/projects/{project_id}/gsc", status_code=204, summary="Unbind the Search Console property"
)
async def gsc_unbind(
    project_id: str,
    owner: int = Depends(seo_owner),
    service: SeoGoogleService = Depends(google_service),
) -> Response:
    await service.gsc_unbind(owner, project_id)
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
    owner: int = Depends(seo_owner),
    service: SeoGoogleService = Depends(google_service),
) -> Any:
    return await service.gsc_report(owner, project_id, **body.model_dump())


@router.post(
    "/projects/{project_id}/gsc/table",
    response_model=GscTable,
    summary="One page of top queries or pages",
)
async def gsc_table(
    project_id: str,
    body: GscTableQuery,
    owner: int = Depends(seo_owner),
    service: SeoGoogleService = Depends(google_service),
) -> Any:
    return await service.gsc_table(owner, project_id, **body.model_dump())


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
    owner: int = Depends(seo_owner),
    service: SeoGoogleService = Depends(google_service),
) -> Any:
    return await service.gsc_inspect(owner, project_id, **body.model_dump())


@router.get(
    "/projects/{project_id}/ga4",
    response_model=Ga4ConnectionState,
    summary="Read the project's Analytics property",
)
async def ga4_connection(
    project_id: str,
    owner: int = Depends(seo_owner),
    service: SeoGoogleService = Depends(google_service),
) -> Any:
    return {"connection": await service.ga4_connection(owner, project_id)}


@router.get(
    "/projects/{project_id}/ga4/properties",
    response_model=Ga4Properties,
    summary="List Analytics properties of the connected Google accounts",
)
async def ga4_properties(
    project_id: str,
    owner: int = Depends(seo_owner),
    service: SeoGoogleService = Depends(google_service),
) -> Any:
    return await service.ga4_properties(owner, project_id)


@router.put(
    "/projects/{project_id}/ga4",
    response_model=Ga4Connection,
    summary="Bind an Analytics property to the project",
    description="The property must be readable by the chosen Google account; its time zone "
    "and currency are stored with the binding.",
)
async def ga4_bind(
    project_id: str,
    body: Ga4Bind,
    owner: int = Depends(seo_owner),
    service: SeoGoogleService = Depends(google_service),
) -> Any:
    return await service.ga4_bind(owner, project_id, **body.model_dump())


@router.delete(
    "/projects/{project_id}/ga4", status_code=204, summary="Unbind the Analytics property"
)
async def ga4_unbind(
    project_id: str,
    owner: int = Depends(seo_owner),
    service: SeoGoogleService = Depends(google_service),
) -> Response:
    await service.ga4_unbind(owner, project_id)
    return Response(status_code=204)


@router.post(
    "/projects/{project_id}/ga4/overview",
    response_model=Ga4OverviewResult,
    summary="Organic Analytics overview",
    description="Organic-search sessions, users, engagement, key events, transactions and "
    "revenue against the previous equal-length period, plus a daily or weekly trend. "
    "Defaults to the 28 days ending yesterday in the property's time zone. Read live; free.",
)
async def ga4_overview(
    project_id: str,
    body: Ga4Overview,
    owner: int = Depends(seo_owner),
    service: SeoGoogleService = Depends(google_service),
) -> Any:
    return await service.ga4_overview(owner, project_id, **body.model_dump())


@router.post(
    "/projects/{project_id}/ga4/report",
    response_model=Ga4ReportResult,
    summary="One page of an Analytics report",
    description="Report kinds: landing_pages, page_performance, key_events, "
    "traffic_acquisition, ecommerce_performance, site_search, audience_breakdown. "
    "Organic Search only unless channel is all. Read live; free.",
)
async def ga4_report(
    project_id: str,
    body: Ga4Report,
    owner: int = Depends(seo_owner),
    service: SeoGoogleService = Depends(google_service),
) -> Any:
    return await service.ga4_report(owner, project_id, **body.model_dump())
