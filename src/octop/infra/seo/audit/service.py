"""Project-scoped site audit commands and reads; the runner does the crawling."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from octop.i18n import tr
from octop.infra.db.repos.seo_audit import SeoAuditRepo
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.seo.audit.rules import ISSUE_SEVERITY, SEVERITY_ORDER, sort_issues
from octop.infra.seo.audit.urls import TargetBlocked, validate_start_url
from octop.infra.seo.models import AuditCreate
from octop.infra.seo.service import SeoService
from octop.infra.utils.ulid import new_ulid

ACTIVE = frozenset({"queued", "running"})


async def _db[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    return await asyncio.get_running_loop().run_in_executor(None, partial(fn, *args, **kwargs))


def _rejected(code: ErrorCode, reason: str, locale: str) -> OctopError:
    """The generic code message plus a specific reason the dashboard appends to it."""
    text = tr(f"seo.audit.errors.{reason}", locale)
    return OctopError.localized(code, message=text, details={"reason": text})


def _bare_host(host: str | None) -> str:
    return (host or "").lower().removeprefix("www.")


def issue_descriptor(issue_type: str, locale: str) -> dict[str, str]:
    base = f"seo.audit.issues.{issue_type}"
    return {
        "title": tr(f"{base}.title", locale),
        "explanation": tr(f"{base}.explanation", locale),
        "how_to_fix": tr(f"{base}.how_to_fix", locale),
    }


def _summary(issues: list[dict[str, Any]], locale: str) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue["issue_type"]] = counts.get(issue["issue_type"], 0) + 1
    rows = [
        {
            "issue_type": issue_type,
            "severity": ISSUE_SEVERITY.get(issue_type, "info"),
            "count": count,
            **issue_descriptor(issue_type, locale),
        }
        for issue_type, count in counts.items()
    ]
    return sorted(rows, key=lambda row: (SEVERITY_ORDER[row["severity"]], -row["count"]))


class SeoAuditService:
    def __init__(self, seo: SeoService) -> None:
        self.seo = seo

    @property
    def repo(self) -> SeoAuditRepo:
        return self.seo.repos.seo_audit_repo

    async def start(
        self,
        user_id: int,
        project_id: str,
        *,
        locale: str = "en",
        own_site_only: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Queue an audit. The same key returns the same audit; one runs per project.

        ``own_site_only`` keeps the crawl on the project's domain (with or without www).
        """
        project = await self.seo._project(user_id, project_id)
        try:
            command = AuditCreate.model_validate(kwargs)
        except ValidationError as exc:
            raise OctopError.localized(ErrorCode.SEO_INVALID_INPUT) from exc
        try:
            url = validate_start_url(command.start_url or project["domain"])
        except TargetBlocked as exc:
            raise _rejected(ErrorCode.SEO_INVALID_INPUT, "not_public", locale) from exc
        except ValueError as exc:
            raise _rejected(ErrorCode.SEO_INVALID_INPUT, "invalid_url", locale) from exc
        if own_site_only and _bare_host(urlsplit(url).hostname) != _bare_host(project["domain"]):
            raise _rejected(ErrorCode.SEO_INVALID_INPUT, "other_site", locale)
        status, audit = await _db(
            self.repo.create,
            {
                "audit_id": new_ulid(),
                "project_id": project_id,
                "started_by": user_id,
                "idempotency_key": command.idempotency_key,
                "requested_url": url,
                "max_pages": command.max_pages,
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
        if status == "active" or audit is None:
            raise _rejected(ErrorCode.SEO_CONFLICT, "busy", locale)
        if status == "replay" and (audit["requested_url"], audit["max_pages"]) != (
            url,
            command.max_pages,
        ):
            raise OctopError.localized(ErrorCode.SEO_CONFLICT)
        return audit

    async def list_audits(self, user_id: int, project_id: str) -> list[dict[str, Any]]:
        await self.seo._project(user_id, project_id)
        return await _db(self.repo.list_audits, project_id)

    async def _audit(self, user_id: int, project_id: str, audit_id: str | None) -> dict[str, Any]:
        await self.seo._project(user_id, project_id)
        audit = (
            await _db(self.repo.get, project_id, audit_id)
            if audit_id
            else await _db(self.repo.latest, project_id)
        )
        if audit is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        return audit

    async def get(
        self, user_id: int, project_id: str, audit_id: str | None = None
    ) -> dict[str, Any]:
        """The audit with issue counts by severity."""
        audit = await self._audit(user_id, project_id, audit_id)
        issues = await _db(self.repo.issues, audit["audit_id"])
        counts = {"critical": 0, "warning": 0, "info": 0}
        for issue in issues:
            counts[issue["severity"]] = counts.get(issue["severity"], 0) + 1
        return audit | {"issue_counts": counts}

    async def issues(
        self,
        user_id: int,
        project_id: str,
        audit_id: str | None = None,
        *,
        severity: str | None = None,
        issue_type: str | None = None,
        locale: str = "en",
    ) -> dict[str, Any]:
        """Per-type summary with how-to-fix copy, and the rows, severity first."""
        audit = await self._audit(user_id, project_id, audit_id)
        rows = [
            row
            for row in await _db(self.repo.issues, audit["audit_id"])
            if (severity is None or row["severity"] == severity)
            and (issue_type is None or row["issue_type"] == issue_type)
        ]
        return {"audit": audit, "summary": _summary(rows, locale), "issues": sort_issues(rows)}

    async def pages(
        self,
        user_id: int,
        project_id: str,
        audit_id: str | None = None,
        *,
        fetch_class: str | None = None,
        status_code: int | None = None,
        url_contains: str = "",
    ) -> dict[str, Any]:
        audit = await self._audit(user_id, project_id, audit_id)
        pages = [
            page
            for page in await _db(self.repo.pages, audit["audit_id"])
            if (fetch_class is None or page["fetch_class"] == fetch_class)
            and (status_code is None or page["status_code"] == status_code)
            and url_contains in page["url"]
        ]
        return {"audit": audit, "pages": pages}

    async def cancel(self, user_id: int, project_id: str, audit_id: str) -> dict[str, Any]:
        """Stop an audit; pages already crawled stay readable."""
        await self._audit(user_id, project_id, audit_id)
        audit = await _db(
            self.repo.request_cancel, project_id, audit_id, datetime.now(UTC).isoformat()
        )
        if audit is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        return audit

    async def delete(self, user_id: int, project_id: str, audit_id: str) -> None:
        """Delete an audit and its results; a running crawl stops at its next write."""
        await self._audit(user_id, project_id, audit_id)
        await _db(self.repo.delete, project_id, audit_id)
