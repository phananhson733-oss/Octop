"""AI visibility: whether AI engines name the project, and cite it, for saved prompts.

Ported from OpenSEO AI visibility (aiVisibilityCollection.ts, aiVisibilityResults.ts,
shared/ai-visibility-results.ts) with manual live checks only: ChatGPT and Gemini through
the DataForSEO LLM scrapers, Google AI Overviews through the organic SERP. A check asks
each prompt of each chosen engine once, saves every answer as it arrives, and runs in the
background while the page polls the project. Claude and Perplexity (the token-priced LLM
Responses API) and scheduled checks are not ported.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from octop.i18n import tr
from octop.infra.db.repos.seo_ai import SeoAiRepo
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.seo.ai_matching import match_brand, owned_domain, prompt_is_branded
from octop.infra.seo.models import AiCheckCreate, AiEnginesPut, AiPromptsAdd
from octop.infra.seo.provider import AI_ENGINES, DataForSeoClient, ProviderFailure
from octop.infra.seo.service import SeoService, _db, _error, _now
from octop.infra.utils.ulid import new_ulid

logger = logging.getLogger(__name__)

# OpenSEO's live price per answer (shared/ai-visibility.ts AI_LIVE_RECORD_COST_USD). An AI
# Overview is a live SERP page plus the async overview load, refunded when none loads.
ANSWER_USD = 0.004
DEFAULT_ENGINES = ["chatgpt", "gemini"]
MAX_PROMPTS = 100
CONCURRENT_ANSWERS = 5
# A run beats on every answer, each at most a 150s call; one silent this long was cut off.
STALE_SECONDS = 360
RUN_LIMIT = 20
SOURCE_LIMIT = 50
BRANDED = ("neutral", "branded", "all")
# Checks started by this process, held so the event loop keeps them alive.
_CHECKS: set[asyncio.Task[None]] = set()


def estimate_usd(prompts: int, engines: list[str]) -> float:
    return round(prompts * len(engines) * ANSWER_USD, 6)


def _rejected(code: ErrorCode, reason: str, locale: str) -> OctopError:
    """The generic code message plus a specific reason the dashboard appends to it."""
    text = tr(f"seo.ai.errors.{reason}", locale)
    return OctopError.localized(code, message=text, details={"reason": text})


def summarize_brands(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mentions and citations over answered rows, one entry per brand, own brand first.

    ``avg_position`` is the brand's average place in the order brands are first named.
    """
    summaries: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["status"] != "answered":
            continue
        named = sorted(
            (brand for brand in row["brands"] if brand["first_mention"] is not None),
            key=lambda brand: brand["first_mention"],
        )
        order = [brand["domain"] for brand in named]
        for brand in row["brands"]:
            summary = summaries.setdefault(
                brand["domain"],
                {
                    "name": brand["name"],
                    "domain": brand["domain"],
                    "own": brand["own"],
                    "answers": 0,
                    "mentions": 0,
                    "citations": 0,
                    "positions": [],
                },
            )
            summary["answers"] += 1
            summary["mentions"] += int(brand["mentioned"])
            summary["citations"] += int(brand["cited"])
            if brand["domain"] in order:
                summary["positions"].append(order.index(brand["domain"]) + 1)
    out = []
    for summary in sorted(summaries.values(), key=lambda s: (not s["own"], s["name"].casefold())):
        positions = summary.pop("positions")
        out.append(
            summary
            | {"avg_position": round(sum(positions) / len(positions), 2) if positions else None}
        )
    return out


def _owner(domain: str, brands: list[dict[str, Any]]) -> str | None:
    brand = next((b for b in brands if owned_domain(domain, b["domain"])), None)
    return None if brand is None else "own" if brand["own"] else "competitor"


def cited_sources(rows: list[dict[str, Any]], brands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Domains the answers cite, most cited first, and whose they are."""
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        for domain in {c["domain"].lower().removeprefix("www.") for c in row["citations"]}:
            found.setdefault(domain, {"domain": domain, "citations": 0, "answers": 0})
            found[domain]["answers"] += 1
        for citation in row["citations"]:
            found[citation["domain"].lower().removeprefix("www.")]["citations"] += 1
    ranked = sorted(found.values(), key=lambda s: (-s["citations"], s["domain"]))
    return [
        source | {"owner": _owner(source["domain"], brands)} for source in ranked[:SOURCE_LIMIT]
    ]


def _gap(row: dict[str, Any]) -> bool:
    """A competitor is named or cited where the own brand is neither."""
    own = next((b for b in row["brands"] if b["own"]), None)
    return (
        own is not None
        and not own["mentioned"]
        and not own["cited"]
        and any(not b["own"] and (b["mentioned"] or b["cited"]) for b in row["brands"])
    )


class SeoAiService:
    def __init__(self, seo: SeoService) -> None:
        self.seo = seo

    @property
    def repo(self) -> SeoAiRepo:
        return self.seo.repos.seo_ai_repo

    async def _settle(self, project_id: str) -> None:
        await _db(self.repo.settle, project_id, int(time.time()) - STALE_SECONDS, _now())

    async def _engines(self, project_id: str) -> list[str]:
        return await _db(self.repo.engines, project_id) or list(DEFAULT_ENGINES)

    async def _brands(self, project: dict[str, Any]) -> list[dict[str, Any]]:
        """The project's own brand first, then its saved competitors."""
        own = project["domain"].removeprefix("www.")
        context = await _db(self.seo.repo.context, project["project_id"], 0)
        return [{"name": project["name"], "domain": own, "own": True}] + [
            {"name": c["name"] or c["domain"], "domain": c["domain"], "own": False}
            for c in context["competitors"]
            if not owned_domain(c["domain"], own)
        ]

    async def results(
        self,
        user_id: int,
        project_id: str,
        *,
        run_id: str | None = None,
        branded: str = "neutral",
        gap: bool = False,
    ) -> dict[str, Any]:
        """A check's answers (the newest check by default) with brand and source summaries.

        Prompts naming the own brand are left out unless ``branded`` asks for them. A failed
        answer or one without an AI answer never counts as the brand being absent.
        """
        project = await self.seo._project(user_id, project_id)
        if branded not in BRANDED:
            raise _error()
        await self._settle(project_id)
        runs = await _db(self.repo.runs, project_id, RUN_LIMIT)
        run = runs[0] if runs else None
        if run_id is not None:
            run = await _db(self.repo.run, project_id, run_id)
            if run is None:
                raise _error(ErrorCode.NOT_FOUND)
        answers = await _db(self.repo.answers, run["run_id"]) if run else []
        scoped = [
            row for row in answers if branded == "all" or row["branded"] == (branded == "branded")
        ]
        rows = [row for row in scoped if _gap(row)] if gap else scoped
        prompts = await _db(self.repo.prompts, project_id)
        engines = await self._engines(project_id)
        brands = await self._brands(project)
        return {
            "settings": {
                "engines": engines,
                "location_code": project["location_code"],
                "language_code": project["language_code"],
                "prompt_count": len(prompts),
                "estimate_usd": estimate_usd(len(prompts), engines),
            },
            "brands": brands,
            "prompts": [
                prompt | {"branded": prompt_is_branded(prompt["prompt"], brands[0])}
                for prompt in prompts
            ],
            "runs": runs,
            "run": run,
            "branded": branded,
            "gap": gap,
            "coverage": {
                "answered": sum(row["status"] == "answered" for row in scoped),
                "no_answer": sum(row["status"] == "no_answer" for row in scoped),
                "failed": sum(row["status"] == "failed" for row in scoped),
            },
            "summaries": summarize_brands(rows),
            "sources": cited_sources(rows, run["brands"] if run else brands),
            "rows": [
                {k: v for k, v in row.items() if k not in {"citations", "run_id", "project_id"}}
                | {"citation_count": len(row["citations"])}
                for row in rows
            ],
        }

    async def set_engines(self, user_id: int, project_id: str, **kwargs: Any) -> dict[str, Any]:
        """Choose the engines the next check asks; a running check keeps its own."""
        await self.seo._project(user_id, project_id)
        try:
            command = AiEnginesPut.model_validate(kwargs)
        except ValueError as exc:
            raise _error() from exc
        engines = [engine for engine in AI_ENGINES if engine in command.engines]
        await _db(self.repo.put_engines, project_id, engines, _now())
        return await self.results(user_id, project_id)

    async def add_prompts(
        self, user_id: int, project_id: str, *, locale: str = "en", **kwargs: Any
    ) -> dict[str, int]:
        await self.seo._project(user_id, project_id)
        try:
            command = AiPromptsAdd.model_validate(kwargs)
        except ValueError as exc:
            raise _error() from exc
        now = _now()
        rows = [
            {"prompt_id": new_ulid(), "topic": command.topic, "prompt": prompt, "created_at": now}
            for prompt in dict.fromkeys(command.prompts)
        ]
        added = await _db(self.repo.add_prompts, project_id, rows, MAX_PROMPTS)
        if added is None:
            raise _rejected(ErrorCode.SEO_INVALID_INPUT, "too_many_prompts", locale)
        return {"added": added}

    async def remove_prompt(self, user_id: int, project_id: str, prompt_id: str) -> None:
        """Stop asking a prompt. Its answers stay with the checks that asked it."""
        await self.seo._project(user_id, project_id)
        if not await _db(self.repo.remove_prompt, project_id, prompt_id):
            raise _error(ErrorCode.NOT_FOUND)

    async def answer(
        self, user_id: int, project_id: str, run_id: str, prompt_id: str, engine: str
    ) -> dict[str, Any]:
        """One saved answer with its text and cited pages, each marked with whose it is."""
        await self.seo._project(user_id, project_id)
        run = await _db(self.repo.run, project_id, run_id)
        found = await _db(self.repo.answer, run_id, prompt_id, engine) if run else None
        if run is None or found is None:
            raise _error(ErrorCode.NOT_FOUND)
        found["citations"] = [
            citation | {"owner": _owner(citation["domain"], run["brands"])}
            for citation in found["citations"]
        ]
        return found

    async def start_check(
        self, user_id: int, project_id: str, *, locale: str = "en", **kwargs: Any
    ) -> dict[str, Any]:
        """Start a paid check of every prompt on every chosen engine; the same key returns
        the same run.

        Refused when the estimate is above ``max_cost_usd``, the amount the admin approved.
        """
        project = await self.seo._project(user_id, project_id)
        try:
            command = AiCheckCreate.model_validate(kwargs)
        except ValueError as exc:
            raise _error() from exc
        prompts = await _db(self.repo.prompts, project_id)
        if not prompts:
            raise _rejected(ErrorCode.SEO_INVALID_INPUT, "no_prompts", locale)
        engines = await self._engines(project_id)
        estimate = estimate_usd(len(prompts), engines)
        if estimate > command.max_cost_usd + 1e-9:
            raise _rejected(ErrorCode.SEO_CONFLICT, "estimate_changed", locale)
        client = await self.seo._client(user_id)
        run: dict[str, Any] = {
            "run_id": new_ulid(),
            "project_id": project_id,
            "idempotency_key": command.idempotency_key,
            "status": "running",
            "started_at": _now(),
            "heartbeat_at": int(time.time()),
            "engines": engines,
            # Answers are matched against the brands as they were when the check started.
            "brands": await self._brands(project),
            "location_code": project["location_code"],
            "language_code": project["language_code"],
            "prompts_total": len(prompts),
            "answers_total": len(prompts) * len(engines),
            "answers_done": 0,
            "answers_failed": 0,
            "cost_usd": 0,
            "cost_known_usd": 0,
            "estimate_usd": estimate,
            "error": None,
            "completed_at": None,
        }
        status, stored = await _db(
            self.repo.start_run, run, int(time.time()) - STALE_SECONDS, _now()
        )
        if status == "active" or stored is None:
            raise _rejected(ErrorCode.SEO_CONFLICT, "busy", locale)
        if status == "replay":
            return stored
        task = asyncio.create_task(
            self._run_check(client, run, prompts), name=f"seo-ai-{run['run_id']}"
        )
        _CHECKS.add(task)
        task.add_done_callback(_CHECKS.discard)
        return dict(run)

    async def _run_check(
        self, client: DataForSeoClient, run: dict[str, Any], prompts: list[dict[str, Any]]
    ) -> None:
        slots = asyncio.Semaphore(CONCURRENT_ANSWERS)
        # One writer at a time, so the stored progress never goes backwards.
        writing = asyncio.Lock()
        costs: list[float | None] = []
        held = True
        own = run["brands"][0]

        async def save(answer: dict[str, Any] | None = None) -> None:
            nonlocal held
            async with writing:
                known = [cost for cost in costs if cost is not None]
                run.update(
                    heartbeat_at=int(time.time()),
                    cost_known_usd=round(sum(known), 6),
                    cost_usd=round(sum(known), 6) if len(known) == len(costs) else None,
                )
                # The run was settled as interrupted elsewhere: buy nothing more.
                if not await _db(self.repo.save_run, dict(run), answer):
                    held = False

        async def ask(prompt: dict[str, Any], engine: str) -> None:
            async with slots:
                if not held:
                    return
                row = {
                    "run_id": run["run_id"],
                    "project_id": run["project_id"],
                    "prompt_id": prompt["prompt_id"],
                    "prompt": prompt["prompt"],
                    "topic": prompt["topic"],
                    "engine": engine,
                    "branded": prompt_is_branded(prompt["prompt"], own),
                }
                try:
                    found, cost, _ = await client.ai_answer(
                        engine=engine,
                        prompt=prompt["prompt"],
                        location_code=run["location_code"],
                        language_code=run["language_code"],
                    )
                except ProviderFailure as exc:
                    # The receipt's task status tells e.g. an endpoint the account lacks.
                    logger.warning(
                        "SEO AI answer %s %s/%s failed: %s %s",
                        run["run_id"],
                        prompt["prompt_id"],
                        engine,
                        exc,
                        exc.receipt,
                    )
                    costs.append(exc.cost)
                    run["answers_done"] += 1
                    run["answers_failed"] += 1
                    run["error"] = str(exc)
                    await save(
                        row
                        | {
                            "status": "failed",
                            "answer_markdown": None,
                            "citations": [],
                            "brands": [],
                            "error": str(exc),
                            "cost_usd": exc.cost,
                            "checked_at": _now(),
                        }
                    )
                    return
                costs.append(cost)
                run["answers_done"] += 1
                await save(
                    row
                    | {
                        "status": "answered" if found["answer_markdown"] else "no_answer",
                        "answer_markdown": found["answer_markdown"],
                        "citations": found["citations"],
                        "brands": [
                            match_brand(found["answer_text"], found["citations"], brand)
                            for brand in run["brands"]
                        ],
                        "error": None,
                        "cost_usd": cost,
                        "checked_at": _now(),
                    }
                )

        async def interrupt() -> None:
            # An answer in flight may have been charged without being saved.
            run.update(status="unknown", error="interrupted", completed_at=_now())
            costs.append(None)
            try:
                await asyncio.shield(save())
            except Exception:
                logger.warning("SEO AI check %s could not be closed", run["run_id"])

        try:
            async with asyncio.TaskGroup() as group:
                for prompt in prompts:
                    for engine in run["engines"]:
                        group.create_task(ask(prompt, engine))
            if held:
                failed = run["answers_failed"] == run["answers_total"]
                run.update(status="failed" if failed else "completed", completed_at=_now())
                await save()
        except Exception:
            logger.warning("SEO AI check %s failed", run["run_id"], exc_info=True)
            await interrupt()
        except BaseException:
            # Shutdown.
            await interrupt()
            raise
