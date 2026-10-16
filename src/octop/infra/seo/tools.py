"""Project-bound report tools; paid keyword requests stay behind the explicit UI action."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from langchain_core.tools import StructuredTool
from langgraph.config import get_config

from octop.infra.errors import ErrorCode, OctopError

if TYPE_CHECKING:
    from octop.infra.db.repos.threads import ThreadRepo
    from octop.infra.seo.service import SeoService

# The harness moves tool results above 20k tokens (~80k chars) to a file and shows
# the model only the first 1000 characters of this one-line JSON, so every result
# stays below this size. Rows that do not fit are read with offset/next_offset.
MAX_RESULT_CHARS = 60_000
_PREVIEW_ROWS = 50
_PAGE_ROWS = 100

_REPORT_GUIDE = """Write one complete self-contained HTML keyword-research report from the returned
research and saved-keyword evidence. Include the business opportunity, a prioritized table,
why each selected term fits, missing metrics and shared-volume caveats, next steps, and an
explicit methodology/source-date section. Treat keyword metrics as evidence, not guaranteed
traffic. Never invent metrics or silently replace unknown values with zero. Distinguish a
saved shortlist from all researched terms. This context shows previews only: when
saved_keywords or research_rows_preview has more rows than shown, read the rest with
seo_list_saved_keywords and seo_list_research_rows, passing next_offset until it is null.
Trend entries are [year, month, search_volume]. Use inline CSS and static HTML/SVG only: no
scripts, forms, remote images, remote CSS, or external data fetches. The document must contain
<html and end with </html>, under 500000 UTF-8 bytes (aim below 80000). Write the actual
analysis, then call seo_save_report with this exact run_id and context_hash, a title <=120
characters and summary <=2500. If saving reports a context conflict, read the context again
and revise the report against the changed evidence before saving; never just substitute a new
hash. Saving creates a private immutable report; it does not publish it or buy any SEO data.
"""


def _metrics(row: dict[str, Any]) -> dict[str, Any]:
    out = {
        key: row.get(key)
        for key in (
            "keyword",
            "search_volume",
            "cpc",
            "competition",
            "keyword_difficulty",
            "intent",
        )
    }
    out["trend"] = [
        [point.get("year"), point.get("month"), point.get("search_volume")]
        for point in row.get("trend") or []
    ]
    return out


def _saved_row(row: dict[str, Any]) -> dict[str, Any]:
    return _metrics(row) | {
        "tags": row.get("tags", []),
        "observed_at": row.get("observed_at"),
        "source_run_id": row.get("source_run_id"),
    }


def _research_rows(run: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"seed": result["seed"]} | _metrics(row)
        for result in run["results"]
        if result["ok"]
        for row in result["rows"]
    ]


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "run_id",
        "status",
        "observed_at",
        "location_code",
        "language_code",
        "cached",
        "source_run_id",
        "cost_usd",
        "cost_known_usd",
        "error",
        "inputs",
    )
    return {key: run.get(key) for key in keys} | {
        "seeds": [
            {
                "seed": result["seed"],
                "ok": result["ok"],
                "source": result["source"],
                "used_fallback": result["used_fallback"],
                "row_count": len(result["rows"]),
                "error": result.get("error"),
            }
            for result in run["results"]
        ]
    }


def _fit(build: Callable[[int], dict[str, Any]], count: int) -> str:
    """Dump ``build(count)``, halving ``count`` until it fits ``MAX_RESULT_CHARS``."""
    while True:
        text = json.dumps(build(count), ensure_ascii=False)
        if len(text) <= MAX_RESULT_CHARS or count <= 1:
            return text
        count //= 2


def _page(items: list[Any], offset: int, key: str, head: dict[str, Any] | None = None) -> str:
    offset = max(offset, 0)

    def build(count: int) -> dict[str, Any]:
        end = min(offset + count, len(items))
        return (head or {}) | {
            "total_count": len(items),
            "offset": offset,
            "next_offset": end if end < len(items) else None,
            key: items[offset:end],
        }

    return _fit(build, _PAGE_ROWS)


def build_seo_tools(
    service: SeoService, bound_agent_id: str, thread_repo: ThreadRepo
) -> list[StructuredTool]:
    """Bind identity outside model arguments and revalidate it after every resume."""

    async def context() -> tuple[int, str, str]:
        try:
            cfg = get_config().get("configurable") or {}
            user_id = int(cfg.get("user", ""))
            thread_id = str(cfg.get("thread_id") or "")
            runtime_agent = str(cfg.get("agent_id") or "")
        except (RuntimeError, TypeError, ValueError) as exc:
            raise OctopError(ErrorCode.FORBIDDEN, "Missing SEO runtime identity") from exc
        if user_id <= 0 or runtime_agent != bound_agent_id or not thread_id:
            raise OctopError(ErrorCode.FORBIDDEN, "Invalid SEO runtime identity")
        thread = await asyncio.get_running_loop().run_in_executor(None, thread_repo.get, thread_id)
        if thread is None or thread.user_id != user_id or thread.agent_id != bound_agent_id:
            raise OctopError(ErrorCode.FORBIDDEN, "SEO thread is not owned by the caller")
        project = await service.project_for_agent(user_id, bound_agent_id)
        if project is None:
            raise OctopError(ErrorCode.FORBIDDEN, "Agent has no owned SEO project")
        return user_id, str(project["project_id"]), thread_id

    async def report_context(run_id: str) -> str:
        user_id, project_id, _thread_id = await context()
        data = await service.report_context(user_id, project_id, run_id)
        saved = [_saved_row(row) for row in data["saved_keywords"]]
        researched = sorted(
            _research_rows(data["run"]),
            key=lambda row: (row["search_volume"] is None, -(row["search_volume"] or 0)),
        )

        def build(count: int) -> dict[str, Any]:
            # Hash and instructions first; the context_hash covers the full evidence.
            return {
                "context_hash": data["context_hash"],
                "run_id": run_id,
                "report_instructions": _REPORT_GUIDE,
                "instructions": data["instructions"],
                "project": data["project"],
                "run": _run_summary(data["run"]),
                "saved_keywords": {
                    "total_count": len(saved),
                    "shown": min(count, len(saved)),
                    "rows": saved[:count],
                },
                "research_rows_preview": {
                    "total_count": len(researched),
                    "shown": min(count, len(researched)),
                    "order": "search_volume descending, unknown last",
                    "rows": researched[:count],
                },
            }

        return _fit(build, _PREVIEW_ROWS)

    async def saved_keywords(search: str = "", offset: int = 0) -> str:
        user_id, project_id, _thread_id = await context()
        rows = await service.keyword_rows(user_id, project_id, search=search)
        return _page([_saved_row(row) for row in rows], offset, "rows")

    async def research_rows(run_id: str, offset: int = 0) -> str:
        user_id, project_id, _thread_id = await context()
        run = await service.get_run(user_id, project_id, run_id)
        return _page(_research_rows(run), offset, "rows", {"run_id": run_id})

    async def research_runs(offset: int = 0) -> str:
        user_id, project_id, _thread_id = await context()
        runs = await service.list_runs(user_id, project_id)
        return _page([_run_summary(run) for run in runs], offset, "runs")

    async def save_report(
        run_id: str, context_hash: str, title: str, summary: str, html: str
    ) -> str:
        user_id, project_id, thread_id = await context()
        data = await service.save_report(
            user_id,
            project_id,
            run_id=run_id,
            context_hash=context_hash,
            title=title,
            summary=summary,
            html=html,
            skill="keyword-research",
            thread_id=thread_id,
        )
        # The app URL contains identifiers, never credentials or a public share token.
        data = {
            **data,
            "workbench_path": f"/workbench/seo-workbench.main?project={project_id}&view=reports",
        }
        return json.dumps(data, ensure_ascii=False)

    async def reports(offset: int = 0) -> str:
        user_id, project_id, _thread_id = await context()
        return _page(await service.list_reports(user_id, project_id), offset, "reports")

    definitions: list[tuple[str, Any, str]] = [
        (
            "seo_get_report_context",
            report_context,
            "Read this agent's owned project's research run and saved keywords. Free SEO data read. "
            "Returns context_hash and report-writing instructions first, a run summary, and "
            "previews of saved keywords and researched rows. Call before generating a keyword "
            "report.",
        ),
        (
            "seo_list_saved_keywords",
            saved_keywords,
            "Read this agent's project's saved keyword shortlist, metrics and source dates; no "
            "paid query. Pass next_offset from the previous result as offset to read more.",
        ),
        (
            "seo_list_research_rows",
            research_rows,
            "Read all researched keyword rows of one research run, with their seed; no paid "
            "query. Pass next_offset from the previous result as offset to read more.",
        ),
        (
            "seo_list_research_runs",
            research_runs,
            "List existing research runs for this agent's project as summaries, including "
            "failures, costs and source dates. Rows are read with seo_list_research_rows.",
        ),
        (
            "seo_save_report",
            save_report,
            "Save complete evidence-backed HTML written by you as a private immutable report. "
            "Does not generate analysis or call a paid provider. Use seo_get_report_context first. "
            "Bound to the caller's current agent, project and thread; no arbitrary project argument.",
        ),
        (
            "seo_list_reports",
            reports,
            "List private report metadata for this agent's project without loading HTML.",
        ),
    ]
    return [
        StructuredTool.from_function(coroutine=fn, name=name, description=description)
        for name, fn, description in definitions
    ]
