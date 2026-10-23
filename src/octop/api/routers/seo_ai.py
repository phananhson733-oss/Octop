"""HTTP adapters for SEO AI visibility (engines, prompts, paid checks and their answers) and
AI search lookups bought one at a time."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from octop.api.deps import require_permission
from octop.api.routers.seo import seo_owner, seo_service
from octop.infra.seo.ai_visibility import SeoAiService
from octop.infra.seo.models import (
    AiCheckCreate,
    AiEnginesPut,
    AiPromptsAdd,
    BrandLookupCreate,
    PromptLookupCreate,
)
from octop.infra.seo.service import SeoService
from octop.infra.utils.locale import resolve_request_locale

router = APIRouter(dependencies=[Depends(require_permission("seo"))])

Engine = Literal["chatgpt", "gemini", "google_ai_overview"]


def ai_service(service: SeoService = Depends(seo_service)) -> SeoAiService:
    return SeoAiService(service)


class AiSettings(BaseModel):
    engines: list[Engine] = Field(description="Engines the next check asks.")
    location_code: int
    language_code: str
    prompt_count: int
    estimate_usd: float = Field(
        description="What the next check costs. Send it as max_cost_usd to approve it."
    )


class AiBrand(BaseModel):
    name: str
    domain: str
    own: bool = Field(description="The project itself; the others are its saved competitors.")


class AiPrompt(BaseModel):
    prompt_id: str
    topic: str
    prompt: str
    created_at: str
    branded: bool = Field(
        description="Names the project's brand or domain; left out of results by default."
    )


class AiRun(BaseModel):
    run_id: str
    status: Literal["running", "completed", "failed", "unknown"] = Field(
        description="unknown: interrupted (a restart); an answer in flight may have been charged."
    )
    started_at: str
    completed_at: str | None
    engines: list[Engine]
    brands: list[AiBrand] = Field(description="The brands answers were matched against.")
    prompts_total: int
    answers_total: int = Field(description="Prompts times engines.")
    answers_done: int = Field(description="Answers saved or failed so far.")
    answers_failed: int
    cost_usd: float | None = Field(description="Supplier cost; null when a call's is unknown.")
    cost_known_usd: float
    estimate_usd: float
    error: str | None


class AiBrandMatch(AiBrand):
    mentioned: bool = Field(description="The answer text names the brand or its domain.")
    cited: bool = Field(description="The answer cites a page on the brand's domain.")
    first_mention: int | None = Field(description="Character offset of the first mention.")


class AiAnswerRow(BaseModel):
    prompt_id: str
    prompt: str
    topic: str
    engine: Engine
    branded: bool
    status: Literal["answered", "no_answer", "failed"] = Field(
        description="no_answer: the engine (or Google, for AI Overviews) showed no AI answer."
    )
    brands: list[AiBrandMatch]
    citation_count: int
    error: str | None
    cost_usd: float | None
    checked_at: str


class AiCoverage(BaseModel):
    answered: int
    no_answer: int
    failed: int = Field(description="Failed answers are not counted as the brand being absent.")


class AiBrandSummary(BaseModel):
    name: str
    domain: str
    own: bool
    answers: int = Field(description="Answered rows counted.")
    mentions: int
    citations: int
    avg_position: float | None = Field(
        description="Average place among the brands, in the order each answer first names them."
    )


class AiSource(BaseModel):
    domain: str
    citations: int
    answers: int = Field(description="Answers citing at least one page on the domain.")
    owner: Literal["own", "competitor"] | None


class AiVisibility(BaseModel):
    settings: AiSettings
    brands: list[AiBrand] = Field(description="The brands the next check matches.")
    prompts: list[AiPrompt]
    runs: list[AiRun] = Field(description="The newest 20 checks, newest first.")
    run: AiRun | None = Field(description="The check shown: the newest unless run_id was sent.")
    branded: Literal["neutral", "branded", "all"]
    gap: bool
    coverage: AiCoverage
    summaries: list[AiBrandSummary]
    sources: list[AiSource] = Field(description="The 50 most cited domains.")
    rows: list[AiAnswerRow]


class AiCitation(BaseModel):
    url: str
    domain: str
    title: str | None
    position: int
    owner: Literal["own", "competitor"] | None


class AiAnswer(BaseModel):
    run_id: str
    prompt_id: str
    prompt: str
    topic: str
    engine: Engine
    branded: bool
    status: Literal["answered", "no_answer", "failed"]
    answer_markdown: str | None = Field(
        description="The answer as the engine wrote it. Model output: show it as plain text."
    )
    citations: list[AiCitation]
    brands: list[AiBrandMatch]
    error: str | None
    cost_usd: float | None
    checked_at: str


class AiPromptsAdded(BaseModel):
    added: int = Field(description="Prompts that were not saved yet.")


LlmModel = Literal["chat_gpt", "claude", "gemini", "perplexity"]


class PromptRequest(BaseModel):
    prompt: str
    models: list[LlmModel]
    web_search: bool = Field(description="Whether the models were allowed to search the web.")
    country: str | None = Field(
        description="ISO 3166-1 alpha-2 country the web search was placed in; null for none."
    )


class PromptCitation(BaseModel):
    url: str
    domain: str | None
    title: str | None


class PromptAnswer(BaseModel):
    model: LlmModel
    model_name: str | None = Field(description="The DataForSEO model name that answered.")
    status: Literal["completed", "failed", "unknown"]
    error: str | None = Field(
        description="unsupported_country (Gemini takes no country, Claude a fixed list) and "
        "unknown_model are refused before any request, at no charge."
    )
    text: str = Field(description="The answer as plain text. Model output: never render as HTML.")
    citations: list[PromptCitation] = Field(description="Cited pages, first cited first, max 25.")
    fan_out_queries: list[str] = Field(description="Searches the model ran, max 20.")
    output_tokens: int | None
    web_search: bool | None = Field(description="Whether the model actually searched the web.")
    cost_usd: float | None


Platform = Literal["chat_gpt", "google"]


class BrandRequest(BaseModel):
    target: str = Field(description="The domain (subdomains count) or brand name looked up.")
    target_type: Literal["domain", "keyword"]
    competitors: list[str] = Field(description="Brands or domains compared for share of voice.")


class BrandPlatform(BaseModel):
    platform: Platform
    status: Literal["completed", "failed"]
    mentions: int | None = Field(description="AI answers mentioning the target.")
    ai_search_volume: int | None = Field(description="Monthly searches of those prompts.")


class BrandMonth(BaseModel):
    year: int
    month: int
    volume: int


class BrandShare(BaseModel):
    label: str
    is_target: bool
    mentions: int | None = Field(description="null: no data, left out of the share.")
    share_pct: float | None


class BrandShareOfVoice(BaseModel):
    platforms: list[Platform] = Field(description="The platforms whose comparison answered.")
    entries: list[BrandShare]


class BrandOverview(BaseModel):
    chatgpt_counted: bool = Field(
        description="ChatGPT data is US English only: it counts toward totals, the trend and "
        "share of voice only when the project's market is US English."
    )
    total_mentions: int | None
    total_ai_search_volume: int | None
    platforms: list[BrandPlatform]
    monthly_volume: list[BrandMonth] = Field(description="The last 12 months, oldest first.")
    share_of_voice: BrandShareOfVoice | None = Field(
        description="null without competitors or when no comparison answered."
    )


class BrandSource(BaseModel):
    url: str
    domain: str | None
    title: str | None


class BrandQuery(BaseModel):
    section: Literal["query"]
    platform: Platform
    question: str = Field(description="A prompt whose AI answer mentions the target.")
    ai_search_volume: int | None
    first_seen_at: str | None
    last_seen_at: str | None
    cited_sources: list[BrandSource] = Field(description="Pages that answer cited, max 10.")
    brands_mentioned: list[str] = Field(description="Brands that answer named, max 20.")


class BrandPageKeyword(BaseModel):
    question: str
    ai_search_volume: int | None


class BrandPage(BaseModel):
    section: Literal["page"]
    platform: Platform
    url: str
    domain: str | None
    mentions: int | None = Field(description="Answers citing this page.")
    captured_volume: int | None = Field(description="Monthly searches of those prompts.")
    keywords: list[BrandPageKeyword] = Field(
        description="Example prompts from the 100-prompt sample that cited it."
    )


BrandRow = Annotated[BrandQuery | BrandPage, Field(discriminator="section")]


class AiLookupSummary(BaseModel):
    lookup_id: str
    project_id: str
    kind: Literal["prompt", "brand"]
    request: PromptRequest | BrandRequest = Field(
        description="What was asked (prompt) or looked up (brand)."
    )
    overview: BrandOverview | None = Field(default=None, description="Brand totals (brand).")
    location_code: int = Field(description="The project's market; scopes reuse only.")
    language_code: str
    status: Literal["running", "completed", "failed", "unknown"] = Field(
        description="completed when every call answered (prompt: a model refused at no charge "
        "does not count); a failed or unknown lookup keeps what the others returned and is "
        "never reused. Each prompt answer has its own status."
    )
    observed_at: str
    item_count: int
    cost_usd: float | None
    cost_known_usd: float = 0
    cached: bool = Field(description="Reused within 12 hours at no charge.")
    source_lookup_id: str | None = None
    error: str | None = None


class AiLookup(AiLookupSummary):
    items: list[PromptAnswer] | list[BrandRow] = Field(
        description="One answer per model asked, in model order (prompt); cited pages, then the "
        "prompts whose answers mention the target, by volume (brand)."
    )


_BASE = "/projects/{project_id}/ai-visibility"
_LOOKUPS = "/projects/{project_id}/ai-lookups"


@router.get(
    _BASE,
    response_model=AiVisibility,
    summary="Read AI visibility",
    description="A check's answers with how often each brand is named and cited, and the "
    "domains cited. No supplier request is made.",
)
async def ai_visibility(
    project_id: str,
    run_id: str | None = Query(default=None, description="Omit for the newest check."),
    branded: Literal["neutral", "branded", "all"] = Query(
        default="neutral", description="neutral leaves out prompts naming the project."
    ),
    gap: bool = Query(
        default=False, description="Only answers naming a competitor but not the project."
    ),
    owner: int = Depends(seo_owner),
    service: SeoAiService = Depends(ai_service),
) -> Any:
    return await service.results(owner, project_id, run_id=run_id, branded=branded, gap=gap)


@router.put(_BASE + "/engines", response_model=AiVisibility, summary="Choose AI engines")
async def put_ai_engines(
    project_id: str,
    body: AiEnginesPut,
    owner: int = Depends(seo_owner),
    service: SeoAiService = Depends(ai_service),
) -> Any:
    return await service.set_engines(owner, project_id, **body.model_dump())


@router.post(
    _BASE + "/prompts",
    response_model=AiPromptsAdded,
    summary="Add AI visibility prompts",
    description="Free. At most 100 prompts per project.",
)
async def add_ai_prompts(
    project_id: str,
    body: AiPromptsAdd,
    request: Request,
    owner: int = Depends(seo_owner),
    service: SeoAiService = Depends(ai_service),
) -> Any:
    return await service.add_prompts(
        owner, project_id, locale=resolve_request_locale(request), **body.model_dump()
    )


@router.delete(
    _BASE + "/prompts/{prompt_id}",
    status_code=204,
    summary="Remove an AI visibility prompt",
    description="Its answers stay with the checks that asked it.",
)
async def remove_ai_prompt(
    project_id: str,
    prompt_id: str,
    owner: int = Depends(seo_owner),
    service: SeoAiService = Depends(ai_service),
) -> Response:
    await service.remove_prompt(owner, project_id, prompt_id)
    return Response(status_code=204)


@router.get(
    _BASE + "/runs/{run_id}/answers/{prompt_id}/{engine}",
    response_model=AiAnswer,
    summary="Read one AI answer",
    description="The saved answer text and every page it cites. No supplier request is made.",
)
async def ai_answer(
    project_id: str,
    run_id: str,
    prompt_id: str,
    engine: Engine,
    owner: int = Depends(seo_owner),
    service: SeoAiService = Depends(ai_service),
) -> Any:
    return await service.answer(owner, project_id, run_id, prompt_id, engine)


@router.post(
    _BASE + "/checks",
    response_model=AiRun,
    status_code=202,
    summary="Buy an AI visibility check",
    description="Requires explicit paid confirmation and max_cost_usd at least the settings' "
    "estimate_usd. Buys one live answer per prompt and engine (ChatGPT and Gemini from the "
    "DataForSEO LLM scrapers, AI Overviews from a live Google SERP), in the background; poll "
    "the project for progress. One check at a time per project. No retries. Reuse the "
    "idempotency key to read an existing attempt.",
)
async def start_ai_check(
    project_id: str,
    body: AiCheckCreate,
    request: Request,
    owner: int = Depends(seo_owner),
    service: SeoAiService = Depends(ai_service),
) -> Any:
    return await service.start_check(
        owner, project_id, locale=resolve_request_locale(request), **body.model_dump()
    )


@router.post(
    _LOOKUPS + "/prompt",
    response_model=AiLookup,
    summary="Ask a prompt of AI models",
    description="Requires explicit paid confirmation. Asks one prompt of 1-4 models (ChatGPT, "
    "Claude, Gemini, Perplexity) at once through DataForSEO LLM Responses, each on the newest "
    "model DataForSEO lists, and waits for the answers (up to about 150 s). A model that "
    "fails keeps the others' answers but fails the lookup, so asking again buys every model "
    "afresh. No retries. The same prompt, models, web search and country within 12 hours is "
    "served from a completed lookup at no charge. Reuse the idempotency key to read an "
    "existing attempt.",
)
async def buy_prompt_lookup(
    project_id: str,
    body: PromptLookupCreate,
    owner: int = Depends(seo_owner),
    service: SeoService = Depends(seo_service),
) -> Any:
    found = await service.prompt_lookup(owner, project_id, **body.model_dump())
    return found | {"item_count": len(found["items"])}


@router.post(
    _LOOKUPS + "/brand",
    response_model=AiLookup,
    summary="Look up a brand's AI mentions",
    description="Requires explicit paid confirmation. Buys DataForSEO LLM Mentions for a domain "
    "or brand name on ChatGPT (US English data) and Google AI Overviews (the project's market): "
    "aggregated metrics, the most cited pages and a 100-prompt sample per platform, plus share "
    "of voice against up to 5 competitors. About $0.80, $1.00 with competitors. No retries. The "
    "same target and competitors within 12 hours are served from a non-empty lookup at no "
    "charge. Reuse the idempotency key to read an existing attempt.",
)
async def buy_brand_lookup(
    project_id: str,
    body: BrandLookupCreate,
    owner: int = Depends(seo_owner),
    service: SeoService = Depends(seo_service),
) -> Any:
    found = await service.brand_lookup(owner, project_id, **body.model_dump())
    return found | {"item_count": len(found["items"])}


@router.get(
    _LOOKUPS,
    response_model=list[AiLookupSummary],
    summary="List AI search lookups",
    description="Newest first, without result rows. No supplier request is made.",
)
async def ai_lookups(
    project_id: str,
    kind: Literal["prompt", "brand"] | None = Query(default=None),
    owner: int = Depends(seo_owner),
    service: SeoService = Depends(seo_service),
) -> Any:
    return await service.list_ai_lookups(owner, project_id, kind=kind)


@router.get(_LOOKUPS + "/{lookup_id}", response_model=AiLookup, summary="Read an AI search lookup")
async def ai_lookup(
    project_id: str,
    lookup_id: str,
    owner: int = Depends(seo_owner),
    service: SeoService = Depends(seo_service),
) -> Any:
    found = await service.get_ai_lookup(owner, project_id, lookup_id)
    return found | {"item_count": len(found["items"])}
