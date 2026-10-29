"""Freeze report evidence and follow audit findings through implementation and recheck."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import ValidationError

from octop.infra.errors import ErrorCode
from octop.infra.seo.audit.rules import (
    META_DESCRIPTION_MAX_CHARS,
    META_DESCRIPTION_MIN_CHARS,
    SLOW_RESPONSE_MS,
    TITLE_MAX_CHARS,
    TITLE_MIN_CHARS,
)
from octop.infra.seo.audit.service import SeoAuditService, issue_descriptor
from octop.infra.seo.models import (
    ActionCreate,
    ActionUpdate,
    EvidencePrepare,
    EvidenceSource,
    Ga4Report,
    GscPerformance,
)
from octop.infra.seo.service import SeoService, _db, _error, _now
from octop.infra.users.identity import User
from octop.infra.utils.ulid import new_ulid

MAX_EVIDENCE_BYTES = 2_000_000
# These page-local rules have direct observations without cross-page truncation/skip gates.
AUTO_VERIFY_TYPES = frozenset(
    {
        "missing-title",
        "missing-meta-description",
        "missing-h1",
        "multiple-h1",
        "title-too-long",
        "title-too-short",
        "meta-description-too-long",
        "meta-description-too-short",
        "heading-order-skip",
        "images-missing-alt",
        "noindex-page",
        "canonicalized-page",
        "slow-response",
    }
)

WORKFLOW_GUIDE = """Start with seo_get_project_context and inventory the available evidence. Record
source dates, scope, missing connections and partial coverage. Use already purchased records;
propose paid data only at the user's request. Unavailable GSC means cannibalization is
unverified, not that basic page inspection must stop. Treat competing pages as candidates,
not proof of harm. Distinguish crawler response times from unmeasured field CWV.
For an audit, search-performance or combined report, call seo_prepare_report with explicit
source IDs (and explicit query parameters for GSC/GA4). It freezes server-collected evidence;
read project background (source_index=-1), the frozen saved-keyword shortlist
(source_index=-2), and every source via seo_read_report_evidence until next_offset is null. Cite source kind,
ID/query, capture date and coverage in the report. To include new evidence, prepare a new
bundle; a frozen bundle does not change when live data changes. Write self-contained static
HTML (no scripts or external resources), <=500000 UTF-8 bytes, title <=120 and summary <=2500
characters; save using seo_save_evidence_report with context_id and context_hash. A successful
save returns the report ID; only then say saved. Append conclusions to the research log.
Use seo_create_action for an audit issue worth fixing. Record implementation evidence before
marking implemented. Run a later audit and call seo_verify_action; incomplete coverage keeps
it needs_review, not verified. Each action has its own history; no automatic website edits.
"""


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _target_satisfied(issue_type: str, page: dict[str, Any]) -> bool:
    """A disappeared issue is insufficient when another error replaced it."""
    if issue_type in {"missing-title", "title-too-long", "title-too-short"}:
        return TITLE_MIN_CHARS <= len(page["title"]) <= TITLE_MAX_CHARS
    if issue_type in {
        "missing-meta-description",
        "meta-description-too-long",
        "meta-description-too-short",
    }:
        return (
            META_DESCRIPTION_MIN_CHARS
            <= len(page["meta_description"])
            <= META_DESCRIPTION_MAX_CHARS
        )
    if issue_type in {"missing-h1", "multiple-h1"}:
        return bool(page["h1_count"] == 1)
    if issue_type == "heading-order-skip":
        order = page["heading_order"]
        return bool(
            page["h1_count"] == 1
            and order
            and all(b - a <= 1 for a, b in zip(order, order[1:], strict=False))
        )
    if issue_type == "images-missing-alt":
        return bool(page["images_missing_alt"] == 0)
    if issue_type == "noindex-page":
        return bool(page["is_indexable"])
    if issue_type == "canonicalized-page":
        return all(
            page[key] in (None, "", page["url"])
            for key in ("canonical_url", "header_canonical_url")
        )
    if issue_type == "slow-response":
        return bool(0 <= page["response_time_ms"] <= SLOW_RESPONSE_MS)
    return False


class SeoWorkflow:
    def __init__(self, seo: SeoService) -> None:
        self.seo = seo
        self.audits = SeoAuditService(seo)

    async def prepare(self, user_id: int, project_id: str, **kwargs: Any) -> dict[str, Any]:
        project = await self.seo._project(user_id, project_id)
        try:
            command = EvidencePrepare.model_validate(kwargs)
        except ValidationError as exc:
            raise _error() from exc
        kinds = {source.kind for source in command.sources}
        if (command.report_type == "technical-audit" and "audit" not in kinds) or (
            command.report_type == "search-performance" and not kinds & {"gsc", "ga4"}
        ):
            raise _error()
        sources = []
        for source in command.sources:
            data = await self._source(user_id, project_id, source)
            sources.append(
                {
                    "ref": source.model_dump(),
                    "captured_at": _now(),
                    "content_hash": _digest(data),
                    "data": data,
                }
            )
        background = await self.seo.project_context(user_id, project_id)
        bundle: dict[str, Any] = {
            "context_id": new_ulid(),
            "project_id": project_id,
            "project": project,
            "report_type": command.report_type,
            "created_at": _now(),
            "project_context": background,
            "saved_keywords": await self.seo.keyword_rows(user_id, project_id),
            "sources": sources,
        }
        if len(json.dumps(bundle, ensure_ascii=False).encode()) > MAX_EVIDENCE_BYTES:
            raise _error()
        bundle["context_hash"] = _digest(bundle)
        await _db(self.seo.repo.save_context, bundle)
        return bundle

    async def _source(
        self, user_id: int, project_id: str, source: EvidenceSource
    ) -> dict[str, Any]:
        from octop.infra.seo.ai_visibility import SeoAiService
        from octop.infra.seo.google.service import SeoGoogleService
        from octop.infra.seo.rank import SeoRankService

        kind, key = source.kind, source.id
        if kind in {"gsc", "ga4"}:
            if key is not None:
                raise _error()
            google = SeoGoogleService(self.seo)
            try:
                if kind == "gsc":
                    query = GscPerformance.model_validate(source.query)
                    return await google.gsc_performance(user_id, project_id, **query.model_dump())
                ga4_query = Ga4Report.model_validate(source.query)
                return await google.ga4_report(user_id, project_id, **ga4_query.model_dump())
            except ValidationError as exc:
                raise _error() from exc
        if not key or source.query:
            raise _error()
        if kind == "audit":
            audit = await self.audits.get(user_id, project_id, key)
            if audit["status"] in {"running", "queued"}:
                raise _error(ErrorCode.SEO_CONFLICT)
            return {
                "audit": audit,
                "issues": await _db(self.audits.repo.issues, key),
                "pages": await _db(self.audits.repo.pages, key),
            }
        if kind == "research":
            data = await self.seo.get_run(user_id, project_id, key)
        elif kind == "serp":
            data = await self.seo.get_serp(user_id, project_id, key)
        elif kind == "lookup":
            data = await self.seo.get_lookup(user_id, project_id, key)
        elif kind == "ai_lookup":
            data = await self.seo.get_ai_lookup(user_id, project_id, key)
        elif kind == "rank":
            data = await SeoRankService(self.seo).results(user_id, project_id, key)
        else:
            ai = SeoAiService(self.seo)
            data = await ai.results(user_id, project_id, run_id=key, branded="all")
            data["answers"] = await _db(ai.repo.answers, key)
        if data.get("status") == "running" or (data.get("run") or {}).get("status") == "running":
            raise _error(ErrorCode.SEO_CONFLICT)
        return data

    async def bundle(self, user_id: int, project_id: str, context_id: str) -> dict[str, Any]:
        await self.seo._project(user_id, project_id)
        bundle = await _db(self.seo.repo.report_bundle, project_id, context_id)
        if bundle is None:
            raise _error(ErrorCode.NOT_FOUND)
        if bundle["context_hash"] != _digest(
            {k: v for k, v in bundle.items() if k != "context_hash"}
        ):
            raise _error(ErrorCode.SEO_REPORT_INTEGRITY)
        return bundle

    async def actions(self, user_id: int, project_id: str) -> list[dict[str, Any]]:
        await self.seo._project(user_id, project_id)
        return await _db(self.seo.repo.actions, project_id)

    async def _actor(self, owner_id: int, actor_id: int | None) -> int:
        """Actor comes from authenticated HTTP/runtime context, never model/body fields."""
        row = await _db(self.seo.repos.user_repo.get, owner_id if actor_id is None else actor_id)
        if row is None or row.disabled or (row.role != "admin" and "seo" not in row.permissions):
            raise _error(ErrorCode.FORBIDDEN)
        identity = User(
            id=row.id,
            username=row.username,
            role=row.role,
            display_name=row.display_name,
            permissions=row.permissions,
        )
        if await self.seo.workspace_user_id(identity) != owner_id:
            raise _error(ErrorCode.FORBIDDEN)
        return row.id

    async def create_action(
        self, user_id: int, project_id: str, *, actor_id: int | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        actor = await self._actor(user_id, actor_id)
        try:
            command = ActionCreate.model_validate(kwargs)
        except ValidationError as exc:
            raise _error() from exc
        audit = await self.audits.get(user_id, project_id, command.audit_id)
        if audit["status"] in {"running", "queued"}:
            raise _error(ErrorCode.SEO_CONFLICT)
        issues = await _db(self.audits.repo.issues, command.audit_id)
        issue = next(
            (
                item
                for item in issues
                if item["issue_type"] == command.issue_type and item["page_url"] == command.page_url
            ),
            None,
        )
        if issue is None:
            raise _error(ErrorCode.NOT_FOUND)
        now = _now()
        value = {
            "action_id": new_ulid(),
            "project_id": project_id,
            "version": 1,
            "source": {
                "audit_id": command.audit_id,
                **issue,
                "labels": {
                    locale: issue_descriptor(command.issue_type, locale)["title"]
                    for locale in ("en", "zh")
                },
            },
            "status": "open",
            "created_at": now,
            "updated_at": now,
            "implementation": None,
            "verification": None,
            "history": [{"status": "open", "at": now, "by": actor, "note": ""}],
        }
        return await _db(self.seo.repo.create_action, value, _digest(command.model_dump()))

    async def _action(self, user_id: int, project_id: str, action_id: str) -> dict[str, Any]:
        found = next(
            (
                row
                for row in await self.actions(user_id, project_id)
                if row["action_id"] == action_id
            ),
            None,
        )
        if found is None:
            raise _error(ErrorCode.NOT_FOUND)
        return found

    async def _save_action(self, value: dict[str, Any], user_id: int, note: str) -> dict[str, Any]:
        version = value["version"]
        if len(value["history"]) >= 100:
            raise _error()
        value["version"] += 1
        value["updated_at"] = _now()
        value["history"].append(
            {
                "status": value["status"],
                "at": value["updated_at"],
                "by": user_id,
                "note": note,
                "verification": value.get("verification"),
            }
        )
        if not await _db(self.seo.repo.update_action, value, version):
            raise _error(ErrorCode.SEO_CONFLICT)
        return value

    async def update_action(
        self,
        user_id: int,
        project_id: str,
        action_id: str,
        *,
        actor_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        actor = await self._actor(user_id, actor_id)
        value = await self._action(user_id, project_id, action_id)
        try:
            command = ActionUpdate.model_validate(kwargs)
        except ValidationError as exc:
            raise _error() from exc
        note = command.note.strip()
        if command.status == "implemented" and not note:
            raise _error()
        value["status"] = command.status
        value["verification"] = None
        value["implementation"] = (
            {"note": note, "at": _now()} if command.status == "implemented" else None
        )
        return await self._save_action(value, actor, note)

    async def verify_action(
        self,
        user_id: int,
        project_id: str,
        action_id: str,
        *,
        audit_id: str,
        actor_id: int | None = None,
    ) -> dict[str, Any]:
        actor = await self._actor(user_id, actor_id)
        value = await self._action(user_id, project_id, action_id)
        if not value["implementation"]:
            raise _error(ErrorCode.SEO_CONFLICT)
        audit = await self.audits.get(user_id, project_id, audit_id)
        source = value["source"]
        pages = await _db(self.audits.repo.pages, audit_id)
        issues = await _db(self.audits.repo.issues, audit_id)
        page = next((p for p in pages if p["url"] == source["page_url"]), None)
        still_present = any(
            p["issue_type"] == source["issue_type"] and p["page_url"] == source["page_url"]
            for p in issues
        )
        complete = (
            audit_id != source["audit_id"]
            and audit["status"] == "completed"
            and audit.get("crawl_complete")
            and not audit.get("sitemap_truncated")
            and not audit.get("rate_limited")
            and page is not None
            and page["status_code"] == 200
            and page.get("is_html")
            and page["fetch_class"] == "ok"
            and not page.get("javascript_shell")
            and not page.get("body_truncated")
            and page["crawled_at"] > value["implementation"]["at"]
        )
        satisfied = page is not None and _target_satisfied(source["issue_type"], page)
        reason = (
            "issue_present"
            if still_present
            else "insufficient_coverage"
            if not complete
            else "target_not_satisfied"
            if not satisfied
            else "clear_in_complete_recheck"
        )
        # Rules with conditional cross-page/render evaluation remain explicitly reviewable.
        if source["issue_type"] not in AUTO_VERIFY_TYPES:
            complete = False
            reason = "manual_review_required" if not still_present else "issue_present"
        value["status"] = (
            "verified" if complete and satisfied and not still_present else "needs_review"
        )
        value["verification"] = {
            "audit_id": audit_id,
            "reason": reason,
            "at": _now(),
            "audit": audit,
            "page": page,
            "matching_issues": [p for p in issues if p["page_url"] == source["page_url"]],
        }
        return await self._save_action(value, actor, "")
