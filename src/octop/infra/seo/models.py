"""Validated SEO commands shared by HTTP and internal callers."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreate(Command):
    name: str = Field(min_length=1, max_length=120)
    domain: str = Field(min_length=1, max_length=253)
    agent_id: str = Field(min_length=1, max_length=100)
    location_code: Literal[2840, 2826, 2124, 2036] = 2840
    language_code: Literal["en"] = "en"


class ProjectUpdate(Command):
    agent_id: str = Field(min_length=1, max_length=100)


class ProviderPut(Command):
    login: str = Field(min_length=1, max_length=300)
    password: str = Field(min_length=1, max_length=1000)


class ResearchCreate(Command):
    keywords: list[str] = Field(min_length=1, max_length=5)
    mode: Literal["auto", "related", "suggestions", "ideas"] = "auto"
    result_limit: Literal[150, 300, 500] = 150
    clickstream: bool = False
    group_keywords: bool = False
    idempotency_key: str = Field(min_length=1, max_length=100)
    confirm_paid: Literal[True]


class KeywordsSave(Command):
    run_id: str
    keywords: list[str] = Field(min_length=1, max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=20)
    tag_mode: Literal["append", "replace"] = "append"


class ReportCreate(Command):
    run_id: str
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(default="", max_length=2500)
    html: str = Field(min_length=1, max_length=500000)
    skill: str = Field(default="keyword-research", min_length=1, max_length=60)


class SerpCreate(Command):
    keyword: str = Field(min_length=1, max_length=200)
    device: Literal["desktop", "mobile"] = "desktop"
    # The default top-20 snapshot, or the full 100 bought when paging past it.
    depth: Literal[20, 100] = 20
    idempotency_key: str = Field(min_length=1, max_length=100)
    confirm_paid: Literal[True]


# Project memory, ported from OpenSEO projectContext.ts. Each op is one strict
# object, so a typo fails validation instead of matching another op.
SECTION_KEYS = ("business_overview", "current_goal", "positioning", "writing_preferences")
Slug = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=60, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"),
]


class CompetitorIn(Command):
    domain: str = Field(min_length=1, max_length=255)
    name: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=500)


class KeyPageIn(Command):
    url: str = Field(min_length=1, max_length=2048)
    # Omitted keeps the stored role, so re-adding a known URL cannot reset it.
    role: Literal["hub", "spoke", "money", "other"] | None = None
    topic: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=500)


class SectionOp(Command):
    section: Literal["business_overview", "current_goal", "positioning", "writing_preferences"]
    # An empty string clears the section.
    content: str


class CustomSectionOp(Command):
    custom_section: Slug
    title: str | None = Field(default=None, min_length=1, max_length=120)
    content: str


class DeleteCustomSectionOp(Command):
    delete_custom_section: Slug


class AddCompetitorsOp(Command):
    add_competitors: list[CompetitorIn] = Field(min_length=1, max_length=100)


class RemoveCompetitorsOp(Command):
    remove_competitors: list[str] = Field(min_length=1, max_length=100)


class AddKeyPagesOp(Command):
    add_key_pages: list[KeyPageIn] = Field(min_length=1, max_length=100)


class RemoveKeyPagesOp(Command):
    remove_key_pages: list[str] = Field(min_length=1, max_length=100)


class LogEntryIn(Command):
    summary: str = Field(min_length=1, max_length=1000)


class AppendResearchLogOp(Command):
    # The entry date is stamped by the server.
    append_research_log: LogEntryIn


class RemoveResearchLogOp(Command):
    remove_research_log: list[str] = Field(min_length=1, max_length=100)


ContextOp = (
    SectionOp
    | CustomSectionOp
    | DeleteCustomSectionOp
    | AddCompetitorsOp
    | RemoveCompetitorsOp
    | AddKeyPagesOp
    | RemoveKeyPagesOp
    | AppendResearchLogOp
    | RemoveResearchLogOp
)


class ContextUpdate(Command):
    updates: list[ContextOp] = Field(min_length=1, max_length=50)


# Site audits. OpenSEO's default crawl is 50 pages; 200 keeps one audit to minutes.
AUDIT_MAX_PAGES = 200


class AuditCreate(Command):
    # Defaults to the project's domain.
    start_url: str | None = Field(default=None, max_length=2048)
    max_pages: int = Field(default=50, ge=10, le=AUDIT_MAX_PAGES)
    idempotency_key: str = Field(min_length=1, max_length=100)


# Google Search Console, ported from OpenSEO d7ea785 searchAnalytics.ts and the
# get_search_console_performance / inspect_urls MCP tool schemas.
GscDimension = Literal["query", "page", "country", "device", "date", "searchAppearance"]
GscDateRange = Literal[
    "last_7_days",
    "last_28_days",
    "last_3_months",
    "last_6_months",
    "last_12_months",
    "last_16_months",
]
GscDate = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]


class GoogleConnect(Command):
    # Dashboard path to come back to after Google's consent screen.
    return_path: str | None = Field(default=None, max_length=2048)


class GscBind(Command):
    grant_id: str = Field(min_length=1, max_length=100)
    site_url: str = Field(min_length=1, max_length=2048)


class GscFilter(Command):
    dimension: GscDimension
    operator: Literal["equals", "notEquals", "contains", "notContains"] = "equals"
    expression: str = Field(min_length=1, max_length=2000)


class GscTextFilter(Command):
    operator: Literal["contains", "equals"] = "contains"
    expression: str = Field(min_length=1, max_length=2000)


class GscReportQuery(Command):
    date_range: GscDateRange = "last_28_days"
    device: Literal["DESKTOP", "MOBILE", "TABLET"] | None = None
    # ISO 3166-1 alpha-3, lower case, as Search Console reports countries.
    country: str | None = Field(default=None, pattern=r"^[a-z]{3}$")
    page_filter: GscTextFilter | None = None
    query_filter: GscTextFilter | None = None


class GscTableQuery(GscReportQuery):
    dimension: Literal["query", "page"]
    page: int = Field(default=1, ge=1, le=1000)
    page_size: int = Field(default=25, ge=1, le=100)


class GscInspect(Command):
    urls: list[str] = Field(min_length=1, max_length=10)
    language_code: str | None = Field(default=None, max_length=20)


class GscPerformance(Command):
    dimensions: list[GscDimension] | None = Field(default=None, min_length=1, max_length=4)
    date_range: GscDateRange | None = None
    start_date: GscDate | None = None
    end_date: GscDate | None = None
    filters: list[GscFilter] | None = Field(default=None, max_length=5)
    row_limit: int = Field(default=250, ge=1, le=1000)
    start_row: int = Field(default=0, ge=0)
    min_position: float | None = Field(default=None, ge=1)
    max_position: float | None = Field(default=None, ge=1)
    min_impressions: int | None = Field(default=None, ge=0)
    search_type: Literal["web", "image", "video", "news", "googleNews", "discover"] | None = None
    data_state: Literal["all", "final"] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "GscPerformance":
        # Search Console rejects searchAppearance next to another dimension, and a
        # half-given explicit range would silently become the default window.
        if self.dimensions and "searchAppearance" in self.dimensions and len(self.dimensions) > 1:
            raise ValueError("searchAppearance must be the only dimension")
        if bool(self.start_date) != bool(self.end_date):
            raise ValueError("give both start_date and end_date, or neither")
        return self
