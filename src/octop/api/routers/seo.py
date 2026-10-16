"""Authenticated HTTP adapters for native SEO workflows."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from octop.api.deps import current_user, get_server, require_admin
from octop.infra.seo.audit.service import SeoAuditService
from octop.infra.seo.models import (
    AuditCreate,
    ContextUpdate,
    KeywordsSave,
    ProjectCreate,
    ProjectUpdate,
    ProviderPut,
    ReportCreate,
    ResearchCreate,
    SerpCreate,
)
from octop.infra.seo.service import SeoService
from octop.infra.server import OctopServer
from octop.infra.users.identity import User
from octop.infra.utils.locale import resolve_request_locale

router = APIRouter(dependencies=[Depends(require_admin())])


class Project(BaseModel):
    project_id: str
    name: str
    domain: str
    agent_id: str | None = Field(
        description="Bound expert; null after that expert was deleted, until it is rebound."
    )
    location_code: int
    language_code: str
    created_at: str


class ProviderStatus(BaseModel):
    configured: bool


class KeywordRow(BaseModel):
    keyword: str
    search_volume: float | None = None
    cpc: float | None = None
    competition: float | None = None
    keyword_difficulty: float | None = None
    intent: str
    trend: list[dict[str, int | float | None]]


class SeedResult(BaseModel):
    seed: str
    ok: bool
    source: str
    used_fallback: bool
    rows: list[KeywordRow]
    error: str | None = None


class ResearchRun(BaseModel):
    model_config = ConfigDict(extra="allow")
    run_id: str
    project_id: str
    status: str
    observed_at: str
    location_code: int
    language_code: str
    results: list[SeedResult]
    cost_usd: float | None
    cost_known_usd: float = 0
    cached: bool
    error: str | None = None


class RunSeedSummary(BaseModel):
    seed: str
    ok: bool
    source: str
    used_fallback: bool
    row_count: int
    error: str | None = None


class RunSummary(BaseModel):
    model_config = ConfigDict(extra="allow")
    run_id: str
    project_id: str
    status: str
    observed_at: str
    location_code: int
    language_code: str
    results: list[RunSeedSummary]
    cost_usd: float | None
    cost_known_usd: float = 0
    cached: bool
    error: str | None = None


class SerpItem(BaseModel):
    type: str = Field(description="SERP element type, e.g. organic, people_also_ask, paid.")
    rank: int | None = Field(description="Position among all SERP elements.")
    organic_rank: int | None = Field(
        description="Position among organic results only, as rank trackers count; null otherwise."
    )
    title: str | None = None
    url: str | None = None
    domain: str | None = None
    description: str | None = None
    etv: float | None = None
    estimated_paid_traffic_cost: float | None = None
    referring_domains: int | None = None
    backlinks: int | None = None


class SerpSummary(BaseModel):
    serp_id: str
    project_id: str
    keyword: str
    location_code: int
    language_code: str
    device: str
    depth: int = Field(description="How many rows the snapshot was crawled for.")
    requested_depth: int
    status: str
    observed_at: str
    item_count: int
    cost_usd: float | None
    cached: bool = Field(description="Reused within 12 hours at no charge.")
    source_serp_id: str | None = None
    error: str | None = None


class SerpSnapshot(SerpSummary):
    items: list[SerpItem]


class ContextSection(BaseModel):
    key: str
    content: str
    updated_at: str
    updated_by: str


class CustomSection(BaseModel):
    slug: str
    title: str | None
    content: str
    updated_at: str
    updated_by: str


class Competitor(BaseModel):
    domain: str
    name: str | None
    notes: str | None
    updated_at: str
    updated_by: str


class KeyPage(BaseModel):
    url: str
    role: str
    topic: str | None
    notes: str | None
    updated_at: str
    updated_by: str


class ResearchLogEntry(BaseModel):
    log_id: str
    entry_date: str
    summary: str
    created_by: str
    created_at: str


class ContextEvidence(BaseModel):
    sections: list[ContextSection]
    missing_sections: list[str] = Field(description="Standard sections nobody filled in yet.")
    custom_sections: list[CustomSection]
    competitors: list[Competitor]
    key_pages: list[KeyPage]


class ProjectContext(ContextEvidence):
    research_log: list[ResearchLogEntry] = Field(description="Newest entries first.")
    research_log_limit: int


class SavedKeyword(KeywordRow):
    keyword_id: str
    location_code: int
    language_code: str
    observed_at: str
    source_run_id: str
    tags: list[str]


class SavedKeywords(BaseModel):
    rows: list[SavedKeyword]
    total_count: int


class SaveCounts(BaseModel):
    saved_count: int
    created_count: int
    updated_count: int


class ReportMetadata(BaseModel):
    report_id: str
    project_id: str
    run_id: str
    title: str
    summary: str
    skill: str
    created_at: str
    content_hash: str
    context_hash: str
    size_bytes: int
    thread_id: str | None = None


class Report(ReportMetadata):
    html: str


class Audit(BaseModel):
    audit_id: str
    project_id: str
    started_by: int
    idempotency_key: str
    requested_url: str
    start_url: str | None = Field(description="Where the crawl began, after redirects.")
    max_pages: int
    status: str = Field(description="queued, running, completed, failed or cancelled.")
    phase: str = Field(description="discovery, crawl or finalize.")
    pages_crawled: int
    pages_total: int = Field(description="Pages this audit expects to crawl, up to max_pages.")
    discovered_count: int = Field(description="URLs found so far through links and sitemaps.")
    sitemap_count: int
    crawl_complete: bool | None = Field(
        description="True when every discovered URL was crawled; null until the crawl ends."
    )
    rate_limited: bool = Field(description="The site kept answering 429 and the crawl stopped.")
    error_code: str | None = Field(description="target_blocked, db_error or unknown.")
    error_detail: str | None
    failed_phase: str | None
    cancel_requested: bool
    created_at: str
    started_at: str | None
    completed_at: str | None


class IssueCounts(BaseModel):
    critical: int
    warning: int
    info: int


class AuditDetail(Audit):
    issue_counts: IssueCounts


class AuditIssue(BaseModel):
    issue_type: str
    severity: str
    page_url: str
    details: dict[str, Any] | None


class AuditIssueType(BaseModel):
    issue_type: str
    severity: str
    count: int
    title: str
    explanation: str
    how_to_fix: str


class AuditIssues(BaseModel):
    audit: Audit
    summary: list[AuditIssueType] = Field(description="One entry per issue type, severity first.")
    issues: list[AuditIssue]


class AuditPage(BaseModel):
    url: str
    status_code: int = Field(description="0 when the request failed.")
    fetch_class: str = Field(
        description="ok (any status the site served itself), blocked (bot protection), "
        "rate_limited or error (no response)."
    )
    redirect_url: str | None
    title: str
    meta_description: str
    canonical_url: str | None
    header_canonical_url: str | None
    robots_meta: str | None
    x_robots_tag: str | None
    og_title: str | None
    og_description: str | None
    og_image: str | None
    h1_count: int
    heading_order: list[int]
    word_count: int
    content_hash: str | None
    is_html: bool
    javascript_shell: bool = Field(
        description="The HTML is an empty app shell; the audit does not run JavaScript."
    )
    images_total: int
    images_missing_alt: int
    internal_link_count: int
    external_link_count: int
    has_structured_data: bool
    hreflang: list[str] = Field(description="hreflang values of alternate links.")
    is_indexable: bool
    response_time_ms: int
    crawl_depth: int | None
    in_sitemap: bool
    crawled_at: str


class AuditPages(BaseModel):
    audit: Audit
    pages: list[AuditPage]


class ReportContext(BaseModel):
    context_hash: str
    project: Project
    run: ResearchRun
    saved_keywords: list[SavedKeyword]
    project_context: ContextEvidence
    instructions: str


def seo_service(server: OctopServer = Depends(get_server)) -> SeoService:
    assert server.services is not None
    return SeoService(server.services.repos)


def audit_service(service: SeoService = Depends(seo_service)) -> SeoAuditService:
    return SeoAuditService(service)


def _snapshot(serp: dict[str, Any]) -> dict[str, Any]:
    return serp | {"item_count": len(serp["items"])}


@router.get("/projects", response_model=list[Project], summary="List your SEO projects")
async def projects(
    user: User = Depends(current_user), service: SeoService = Depends(seo_service)
) -> Any:
    return await service.list_projects(user.id)


@router.post("/projects", response_model=Project, summary="Create an agent-bound SEO project")
async def create_project(
    body: ProjectCreate,
    user: User = Depends(current_user),
    service: SeoService = Depends(seo_service),
    server: OctopServer = Depends(get_server),
) -> Any:
    project = await service.create_project(user.id, **body.model_dump())
    if server.app_runtime is not None:
        await server.app_runtime.agent_registry.reload(body.agent_id)
    return project


@router.patch(
    "/projects/{project_id}",
    response_model=Project,
    summary="Bind an SEO project to another expert",
    description="Rebinds a project, for example after its expert was deleted. The target must be "
    "your own unshared expert without another SEO project. The Agent tools move with the binding.",
)
async def update_project(
    project_id: str,
    body: ProjectUpdate,
    user: User = Depends(current_user),
    service: SeoService = Depends(seo_service),
    server: OctopServer = Depends(get_server),
) -> Any:
    project, previous = await service.bind_agent(user.id, project_id, **body.model_dump())
    if server.app_runtime is not None:
        for agent_id in {body.agent_id} | ({previous} if previous else set()):
            await server.app_runtime.agent_registry.reload(agent_id)
    return project


@router.get("/provider", response_model=ProviderStatus, summary="Check SEO provider configuration")
async def provider(
    user: User = Depends(current_user), service: SeoService = Depends(seo_service)
) -> Any:
    return await service.provider_status(user.id)


@router.put(
    "/provider", response_model=ProviderStatus, summary="Store encrypted DataForSEO credentials"
)
async def put_provider(
    body: ProviderPut,
    user: User = Depends(current_user),
    service: SeoService = Depends(seo_service),
) -> Any:
    return await service.set_provider(user.id, **body.model_dump())


@router.post(
    "/projects/{project_id}/research",
    response_model=ResearchRun,
    summary="Run paid keyword research",
    description="Requires explicit paid confirmation. Calls DataForSEO directly without retries. Reuse the idempotency key to read an existing attempt.",
)
async def research(
    project_id: str,
    body: ResearchCreate,
    user: User = Depends(current_user),
    service: SeoService = Depends(seo_service),
) -> Any:
    return await service.research(user.id, project_id, **body.model_dump())


@router.get(
    "/projects/{project_id}/runs",
    response_model=list[RunSummary],
    summary="List keyword research runs",
    description="Newest first, with per-seed status and row counts but no keyword rows. "
    "Read one run's rows with the run endpoint.",
)
async def runs(
    project_id: str, user: User = Depends(current_user), service: SeoService = Depends(seo_service)
) -> Any:
    return await service.list_runs(user.id, project_id)


@router.get(
    "/projects/{project_id}/runs/{run_id}",
    response_model=ResearchRun,
    summary="Read a keyword research attempt",
)
async def run(
    project_id: str,
    run_id: str,
    user: User = Depends(current_user),
    service: SeoService = Depends(seo_service),
) -> Any:
    return await service.get_run(user.id, project_id, run_id)


@router.post(
    "/projects/{project_id}/serps",
    response_model=SerpSnapshot,
    summary="Buy a Google SERP snapshot",
    description="Requires explicit paid confirmation. Calls DataForSEO SERP live directly without "
    "retries, at depth 20 or 100. The same keyword, device and market within 12 hours is served "
    "from a non-empty snapshot at least as deep, at no charge. Reuse the idempotency key to read "
    "an existing attempt.",
)
async def buy_serp(
    project_id: str,
    body: SerpCreate,
    user: User = Depends(current_user),
    service: SeoService = Depends(seo_service),
) -> Any:
    return _snapshot(await service.serp(user.id, project_id, **body.model_dump()))


@router.get(
    "/projects/{project_id}/serps",
    response_model=list[SerpSummary],
    summary="List SERP snapshots",
    description="Newest first, without result rows. No supplier request is made.",
)
async def serps(
    project_id: str,
    keyword: str | None = None,
    user: User = Depends(current_user),
    service: SeoService = Depends(seo_service),
) -> Any:
    return await service.list_serps(user.id, project_id, keyword=keyword)


@router.get(
    "/projects/{project_id}/serps/{serp_id}",
    response_model=SerpSnapshot,
    summary="Read a SERP snapshot",
)
async def serp(
    project_id: str,
    serp_id: str,
    user: User = Depends(current_user),
    service: SeoService = Depends(seo_service),
) -> Any:
    return _snapshot(await service.get_serp(user.id, project_id, serp_id))


@router.get(
    "/projects/{project_id}/context",
    response_model=ProjectContext,
    summary="Read project background",
    description="Business overview, goal, positioning, writing preferences, custom sections, "
    "competitors, key pages and the newest research log entries.",
)
async def project_context(
    project_id: str, user: User = Depends(current_user), service: SeoService = Depends(seo_service)
) -> Any:
    return await service.project_context(user.id, project_id)


@router.patch(
    "/projects/{project_id}/context",
    response_model=ProjectContext,
    summary="Update project background",
    description="Applies up to 50 ops in order and atomically: a rejected op writes nothing. "
    "Sections hold up to 4000 characters, a project up to 20 custom sections, 100 competitors "
    "and 100 key pages. Competitors upsert by domain and key pages by URL.",
)
async def update_project_context(
    project_id: str,
    body: ContextUpdate,
    user: User = Depends(current_user),
    service: SeoService = Depends(seo_service),
) -> Any:
    updates = [op.model_dump(exclude_none=True) for op in body.updates]
    return await service.update_context(user.id, project_id, updates, author="user")


@router.post(
    "/projects/{project_id}/keywords",
    response_model=SaveCounts,
    summary="Save researched keywords atomically",
    description="Metrics and observation dates come from the owned source run. No supplier request is made.",
)
async def save_keywords(
    project_id: str,
    body: KeywordsSave,
    user: User = Depends(current_user),
    service: SeoService = Depends(seo_service),
) -> Any:
    return await service.save_keywords(user.id, project_id, **body.model_dump())


@router.get(
    "/projects/{project_id}/keywords",
    response_model=SavedKeywords,
    summary="List saved SEO keywords",
)
async def keywords(
    project_id: str,
    search: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user: User = Depends(current_user),
    service: SeoService = Depends(seo_service),
) -> Any:
    return await service.list_keywords(
        user.id, project_id, search=search, page=page, page_size=page_size
    )


@router.get(
    "/projects/{project_id}/report-context",
    response_model=ReportContext,
    summary="Read evidence for Agent report generation",
)
async def report_context(
    project_id: str,
    run_id: str,
    user: User = Depends(current_user),
    service: SeoService = Depends(seo_service),
) -> Any:
    return await service.report_context(user.id, project_id, run_id)


@router.post(
    "/projects/{project_id}/reports",
    response_model=ReportMetadata,
    summary="Save an immutable private HTML report",
    description="Persists supplied HTML up to 500000 UTF-8 bytes and source evidence; does not invoke a model or buy data.",
)
async def save_report(
    project_id: str,
    body: ReportCreate,
    user: User = Depends(current_user),
    service: SeoService = Depends(seo_service),
) -> Any:
    return await service.save_report(user.id, project_id, **body.model_dump())


@router.get(
    "/projects/{project_id}/reports",
    response_model=list[ReportMetadata],
    summary="List private report metadata",
)
async def reports(
    project_id: str, user: User = Depends(current_user), service: SeoService = Depends(seo_service)
) -> Any:
    return await service.list_reports(user.id, project_id)


@router.get(
    "/projects/{project_id}/reports/{report_id}",
    response_model=Report,
    summary="Read a private report with integrity verification",
)
async def report(
    project_id: str,
    report_id: str,
    response: Response,
    user: User = Depends(current_user),
    service: SeoService = Depends(seo_service),
) -> Any:
    response.headers["Cache-Control"] = "no-store"
    return await service.get_report(user.id, project_id, report_id)


@router.post(
    "/projects/{project_id}/audits",
    response_model=Audit,
    summary="Start a technical site audit",
    description="Queues a crawl of up to max_pages pages (10-200) of one public website, "
    "starting at start_url or the project domain. Free: no paid provider is called. Follows "
    "robots.txt and the sitemaps it lists, crawls one page at a time and does not run "
    "JavaScript. One audit runs per project; reuse the idempotency key to read the same audit.",
)
async def start_audit(
    project_id: str,
    body: AuditCreate,
    request: Request,
    user: User = Depends(current_user),
    service: SeoAuditService = Depends(audit_service),
    server: OctopServer = Depends(get_server),
) -> Any:
    audit = await service.start(
        user.id, project_id, locale=resolve_request_locale(request), **body.model_dump()
    )
    if server.app_runtime is not None and server.app_runtime.seo_audits is not None:
        server.app_runtime.seo_audits.submit(audit["audit_id"])
    return audit


@router.get(
    "/projects/{project_id}/audits",
    response_model=list[Audit],
    summary="List site audits",
    description="Newest first, up to 100.",
)
async def audits(
    project_id: str,
    user: User = Depends(current_user),
    service: SeoAuditService = Depends(audit_service),
) -> Any:
    return await service.list_audits(user.id, project_id)


@router.get(
    "/projects/{project_id}/audits/{audit_id}",
    response_model=AuditDetail,
    summary="Read a site audit's progress",
)
async def audit(
    project_id: str,
    audit_id: str,
    user: User = Depends(current_user),
    service: SeoAuditService = Depends(audit_service),
) -> Any:
    return await service.get(user.id, project_id, audit_id)


@router.get(
    "/projects/{project_id}/audits/{audit_id}/issues",
    response_model=AuditIssues,
    summary="Read a site audit's issues",
    description="Issues found so far, with an explanation and fix per issue type in the "
    "request language. A running audit adds cross-page issues (duplicates, redirects, broken "
    "links, orphans) when its crawl ends.",
)
async def audit_issues(
    project_id: str,
    audit_id: str,
    request: Request,
    severity: str | None = Query(None, pattern="^(critical|warning|info)$"),
    issue_type: str | None = None,
    user: User = Depends(current_user),
    service: SeoAuditService = Depends(audit_service),
) -> Any:
    return await service.issues(
        user.id,
        project_id,
        audit_id,
        severity=severity,
        issue_type=issue_type,
        locale=resolve_request_locale(request),
    )


@router.get(
    "/projects/{project_id}/audits/{audit_id}/pages",
    response_model=AuditPages,
    summary="Read a site audit's crawled pages",
)
async def audit_pages(
    project_id: str,
    audit_id: str,
    fetch_class: str | None = None,
    status_code: int | None = None,
    url_contains: str = "",
    user: User = Depends(current_user),
    service: SeoAuditService = Depends(audit_service),
) -> Any:
    return await service.pages(
        user.id,
        project_id,
        audit_id,
        fetch_class=fetch_class,
        status_code=status_code,
        url_contains=url_contains,
    )


@router.post(
    "/projects/{project_id}/audits/{audit_id}/cancel",
    response_model=Audit,
    summary="Cancel a site audit",
    description="A queued audit is cancelled at once; a running one stops after the page it is "
    "fetching. Pages and issues found so far stay readable.",
)
async def cancel_audit(
    project_id: str,
    audit_id: str,
    user: User = Depends(current_user),
    service: SeoAuditService = Depends(audit_service),
) -> Any:
    return await service.cancel(user.id, project_id, audit_id)


@router.delete(
    "/projects/{project_id}/audits/{audit_id}",
    status_code=204,
    summary="Delete a site audit and its results",
)
async def delete_audit(
    project_id: str,
    audit_id: str,
    user: User = Depends(current_user),
    service: SeoAuditService = Depends(audit_service),
) -> Response:
    await service.delete(user.id, project_id, audit_id)
    return Response(status_code=204)
