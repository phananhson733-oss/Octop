"""Authenticated HTTP adapters for native SEO workflows."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict

from octop.api.deps import current_user, get_server
from octop.infra.seo.models import (
    KeywordsSave,
    ProjectCreate,
    ProviderPut,
    ReportCreate,
    ResearchCreate,
)
from octop.infra.seo.service import SeoService
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

router = APIRouter()


class Project(BaseModel):
    project_id: str
    name: str
    domain: str
    agent_id: str
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


class ReportContext(BaseModel):
    context_hash: str
    project: Project
    run: ResearchRun
    saved_keywords: list[SavedKeyword]
    instructions: str


def seo_service(server: OctopServer = Depends(get_server)) -> SeoService:
    assert server.services is not None
    return SeoService(server.services.repos)


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
    response_model=list[ResearchRun],
    summary="List keyword research evidence",
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
