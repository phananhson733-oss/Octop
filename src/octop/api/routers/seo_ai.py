"""HTTP adapters for SEO AI visibility: engines, prompts, paid checks and their answers."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from octop.api.deps import require_permission
from octop.api.routers.seo import seo_owner, seo_service
from octop.infra.seo.ai_visibility import SeoAiService
from octop.infra.seo.models import AiCheckCreate, AiEnginesPut, AiPromptsAdd
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


_BASE = "/projects/{project_id}/ai-visibility"


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
