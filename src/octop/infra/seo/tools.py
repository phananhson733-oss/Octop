"""Project-bound report tools; paid keyword requests stay behind the explicit UI action."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from langchain_core.tools import StructuredTool
from langgraph.config import get_config

from octop.infra.errors import ErrorCode, OctopError

if TYPE_CHECKING:
    from octop.infra.db.repos.threads import ThreadRepo
    from octop.infra.seo.service import SeoService

_REPORT_GUIDE = """Write one complete self-contained HTML keyword-research report from the returned
research and saved-keyword evidence. Include the business opportunity, a prioritized table,
why each selected term fits, missing metrics and shared-volume caveats, next steps, and an
explicit methodology/source-date section. Treat keyword metrics as evidence, not guaranteed
traffic. Never invent metrics or silently replace unknown values with zero. Distinguish a
saved shortlist from all researched terms. Use inline CSS and static HTML/SVG only: no scripts,
forms, remote images, remote CSS, or external data fetches. The document must contain <html
and end with </html>, under 500000 UTF-8 bytes (aim below 80000). Write the actual analysis,
then call seo_save_report with this exact run_id and context_hash, a title <=120 characters
and summary <=2500. If saving reports a context conflict, read the context again and revise
the report against the changed evidence before saving; never just substitute a new hash.
Saving creates a private immutable report; it does not publish it or buy any SEO data.
"""


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
        return json.dumps({**data, "report_instructions": _REPORT_GUIDE}, ensure_ascii=False)

    async def saved_keywords(search: str = "", page: int = 1) -> str:
        user_id, project_id, _thread_id = await context()
        data = await service.list_keywords(
            user_id, project_id, search=search, page=page, page_size=50
        )
        return json.dumps(data, ensure_ascii=False)

    async def research_runs() -> str:
        user_id, project_id, _thread_id = await context()
        return json.dumps(await service.list_runs(user_id, project_id), ensure_ascii=False)

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

    async def reports() -> str:
        user_id, project_id, _thread_id = await context()
        return json.dumps(await service.list_reports(user_id, project_id), ensure_ascii=False)

    definitions: list[tuple[str, Any, str]] = [
        (
            "seo_get_report_context",
            report_context,
            "Read this agent's owned project's research run and saved keywords. Free SEO data read. "
            "Includes report-writing instructions. Call before generating a keyword report.",
        ),
        (
            "seo_list_saved_keywords",
            saved_keywords,
            "Read this agent's project's saved keyword shortlist, metrics and source dates; no paid query.",
        ),
        (
            "seo_list_research_runs",
            research_runs,
            "List existing research runs for this agent's project, including failures and source dates.",
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
