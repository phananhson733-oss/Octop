"""Tenant-scoped SEO workflow orchestration; provider output is the metric authority."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from octop.i18n import tr
from octop.infra.connectors.crypto import decrypt_credentials, encrypt_credentials
from octop.infra.db.services import RepoBundle
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.seo.models import (
    KeywordsSave,
    ProjectCreate,
    ProviderPut,
    ReportCreate,
    ResearchCreate,
)
from octop.infra.seo.provider import (
    DataForSeoClient,
    ProviderFailure,
    interleave,
    normalize_keyword,
)
from octop.infra.users.identity import Role
from octop.infra.utils.ulid import new_ulid


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _error(code: ErrorCode = ErrorCode.SEO_INVALID_INPUT) -> OctopError:
    return OctopError.localized(code)


async def _db[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    return await asyncio.get_running_loop().run_in_executor(None, partial(fn, *args, **kwargs))


class SeoService:
    def __init__(self, repos: RepoBundle) -> None:
        self.repos = repos
        self.repo = repos.seo_repo

    async def list_projects(self, user_id: int) -> list[dict[str, Any]]:
        return await _db(self.repo.projects, user_id)

    async def _project(self, user_id: int, project_id: str) -> dict[str, Any]:
        # SEO is admin-only. HTTP checks it too; Agent tools only come through here.
        user = await _db(self.repos.user_repo.get, user_id)
        if user is None or user.role != Role.ADMIN:
            raise _error(ErrorCode.FORBIDDEN)
        project = next(
            (p for p in await self.list_projects(user_id) if p["project_id"] == project_id), None
        )
        if project is None:
            raise _error(ErrorCode.NOT_FOUND)
        agent = await _db(self.repos.agent_repo.get, project["agent_id"])
        if agent is None or agent.user_id != user_id or agent.is_shared or agent.kind != "expert":
            raise _error(ErrorCode.FORBIDDEN)
        return project

    async def project_for_agent(self, user_id: int, agent_id: str) -> dict[str, Any] | None:
        project = next(
            (p for p in await self.list_projects(user_id) if p["agent_id"] == agent_id), None
        )
        if project is None:
            return None
        return await self._project(user_id, project["project_id"])

    async def create_project(
        self,
        user_id: int,
        *,
        name: str,
        domain: str,
        agent_id: str,
        location_code: int = 2840,
        language_code: str = "en",
    ) -> dict[str, Any]:
        try:
            parsed = ProjectCreate.model_validate(
                {
                    "name": name,
                    "domain": domain,
                    "agent_id": agent_id,
                    "location_code": location_code,
                    "language_code": language_code,
                }
            )
        except ValidationError as exc:
            raise _error() from exc
        name = parsed.name.strip()
        host = urlsplit(domain if "://" in domain else "https://" + domain)
        if (
            not name
            or host.scheme not in {"http", "https"}
            or not host.hostname
            or host.username
            or host.password
        ):
            raise _error()
        domain = host.hostname.lower()
        if not re.fullmatch(r"[a-z0-9.-]+", domain) or "." not in domain:
            raise _error()
        agent = await _db(self.repos.agent_repo.get, agent_id)
        if agent is None or agent.user_id != user_id or agent.is_shared or agent.kind != "expert":
            raise _error(ErrorCode.FORBIDDEN)
        project = {
            "project_id": new_ulid(),
            "name": name,
            "domain": domain,
            "agent_id": agent_id,
            "location_code": location_code,
            "language_code": language_code,
            "created_at": _now(),
        }
        if not await _db(self.repo.create_project, user_id, project):
            raise _error(ErrorCode.SEO_CONFLICT)
        return project

    async def provider_status(self, user_id: int) -> dict[str, bool]:
        return {
            "configured": bool(await _db(self.repos.settings_repo.get, f"seo.provider.{user_id}"))
        }

    async def set_provider(self, user_id: int, *, login: str, password: str) -> dict[str, bool]:
        try:
            value = ProviderPut(login=login.strip(), password=password)
        except ValidationError as exc:
            raise _error() from exc
        encrypted = await _db(encrypt_credentials, self.repos.secret_repo, value.model_dump())
        await _db(
            self.repos.settings_repo.set, f"seo.provider.{user_id}", encrypted.decode("ascii")
        )
        return {"configured": True}

    async def _client(self, user_id: int) -> DataForSeoClient:
        raw = await _db(self.repos.settings_repo.get, f"seo.provider.{user_id}")
        if not raw:
            raise _error(ErrorCode.SEO_PROVIDER_REQUIRED)
        value = await _db(decrypt_credentials, self.repos.secret_repo, raw.encode("ascii"))
        return DataForSeoClient(value["login"], value["password"])

    async def list_runs(self, user_id: int, project_id: str) -> list[dict[str, Any]]:
        await self._project(user_id, project_id)
        runs = await _db(self.repo.runs, project_id)
        for run in runs:
            # Bounded request has at most 15 calls, each 60s, with auto legs concurrent.
            # Stale rows remain evidence and are never automatically purchased again.
            if run["status"] == "running" and datetime.fromisoformat(
                run["observed_at"]
            ) < datetime.now(UTC) - timedelta(minutes=20):
                run.update(status="unknown", error="interrupted")
                await _db(self.repo.update_run, run)
            run["calls"] = await _db(self.repo.calls, run["run_id"])
            if not run.get("cached"):
                costs = [call["cost_usd"] for call in run["calls"]]
                run["cost_known_usd"] = sum(cost for cost in costs if cost is not None)
                run["cost_usd"] = (
                    sum(costs) if costs and all(c is not None for c in costs) else None
                )
        return runs

    async def get_run(self, user_id: int, project_id: str, run_id: str) -> dict[str, Any]:
        result = next(
            (r for r in await self.list_runs(user_id, project_id) if r["run_id"] == run_id), None
        )
        if result is None:
            raise _error(ErrorCode.NOT_FOUND)
        return result

    async def research(self, user_id: int, project_id: str, **kwargs: Any) -> dict[str, Any]:
        project = await self._project(user_id, project_id)
        try:
            command = ResearchCreate.model_validate(kwargs)
        except ValidationError as exc:
            raise _error() from exc
        seeds = list(dict.fromkeys(normalize_keyword(k) for k in command.keywords if k.strip()))
        if not seeds or any(len(seed) > 200 for seed in seeds):
            raise _error()
        inputs = command.model_dump(exclude={"idempotency_key", "confirm_paid"}) | {
            "keywords": seeds,
            "location_code": project["location_code"],
            "language_code": project["language_code"],
        }
        input_hash = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
        previous = await _db(self.repo.run_for_key, project_id, command.idempotency_key)
        if previous:
            if previous["input_hash"] != input_hash:
                raise _error(ErrorCode.SEO_CONFLICT)
            return await self.get_run(user_id, project_id, previous["run"]["run_id"])
        cached = await _db(self.repo.cached_run, project_id, input_hash)
        if cached and datetime.fromisoformat(cached["observed_at"]) < datetime.now(UTC) - timedelta(
            days=1
        ):
            cached = None
        client = None if cached else await self._client(user_id)
        run: dict[str, Any] = {
            "run_id": new_ulid(),
            "project_id": project_id,
            "status": "running",
            "observed_at": _now(),
            "location_code": project["location_code"],
            "language_code": project["language_code"],
            "results": [],
            "cost_usd": None,
            "cost_known_usd": 0,
            "cached": False,
            "inputs": inputs,
        }
        if cached:
            # Cache copies retain the original observation date, never extend freshness.
            run.update(
                status="completed",
                observed_at=cached["observed_at"],
                results=cached["results"],
                cost_usd=0,
                cached=True,
                source_run_id=cached["run_id"],
            )
        if not await _db(self.repo.start_run, run, command.idempotency_key, input_hash):
            previous = await _db(self.repo.run_for_key, project_id, command.idempotency_key)
            assert previous is not None
            if previous["input_hash"] != input_hash:
                raise _error(ErrorCode.SEO_CONFLICT)
            return await self.get_run(user_id, project_id, previous["run"]["run_id"])
        if cached:
            return run
        assert client is not None
        try:
            for seed in seeds:
                result = await self._seed(client, run, seed, command)
                run["results"].append(result)
                await _db(self.repo.update_run, run)
            successful = sum(result["ok"] for result in run["results"])
            run["status"] = (
                "completed" if successful == len(seeds) else "partial" if successful else "failed"
            )
            calls = await _db(self.repo.calls, run["run_id"])
            if any(call["status"] == "unknown" for call in calls):
                run["status"] = "unknown"
            run["calls"] = calls
            costs = [call["cost_usd"] for call in calls]
            run["cost_known_usd"] = sum(c for c in costs if c is not None)
            run["cost_usd"] = sum(costs) if all(c is not None for c in costs) else None
            await _db(self.repo.update_run, run)
        except BaseException:
            run.update(status="unknown", error="interrupted")
            await asyncio.shield(_db(self.repo.update_run, run))
            raise
        return run

    async def _seed(
        self, client: DataForSeoClient, run: dict[str, Any], seed: str, command: ResearchCreate
    ) -> dict[str, Any]:
        async def fetch(source: str, limit: int) -> list[dict[str, Any]]:
            call_id = new_ulid()
            await _db(
                self.repo.call_start,
                {
                    "call_id": call_id,
                    "run_id": run["run_id"],
                    "source": source,
                    "seed": seed,
                    "created_at": _now(),
                },
            )
            try:
                rows, cost, receipt = await client.fetch(
                    source=source,
                    seed=seed,
                    location_code=run["location_code"],
                    language_code=run["language_code"],
                    limit=limit,
                    clickstream=command.clickstream,
                    group_keywords=command.group_keywords,
                )
            except ProviderFailure as exc:
                await _db(
                    self.repo.call_finish,
                    call_id,
                    "unknown" if exc.unknown else "failed",
                    exc.cost,
                    str(exc),
                    exc.receipt,
                )
                raise
            await _db(self.repo.call_finish, call_id, "completed", cost, None, receipt)
            return rows

        source = "blended" if command.mode == "auto" else command.mode
        try:
            fallback = False
            if command.mode == "auto":
                legs = await asyncio.gather(
                    fetch("suggestions", (command.result_limit + 1) // 2),
                    fetch("ideas", (command.result_limit + 1) // 2),
                    return_exceptions=True,
                )
                for leg in legs:
                    if isinstance(leg, BaseException):
                        raise leg
                first, second = legs
                assert isinstance(first, list) and isinstance(second, list)
                rows = interleave(first, second, command.result_limit)
                if sum(row["keyword"] != seed for row in rows) < 5:
                    rows = interleave(
                        rows, await fetch("related", command.result_limit), command.result_limit
                    )
                    fallback = True
            else:
                rows = await fetch(command.mode, command.result_limit)
            return {
                "seed": seed,
                "ok": True,
                "source": source,
                "used_fallback": fallback,
                "rows": rows,
            }
        except ProviderFailure as exc:
            return {
                "seed": seed,
                "ok": False,
                "source": source,
                "used_fallback": False,
                "rows": [],
                "error": str(exc),
            }

    async def save_keywords(self, user_id: int, project_id: str, **kwargs: Any) -> dict[str, int]:
        try:
            command = KeywordsSave.model_validate(kwargs)
        except ValidationError as exc:
            raise _error() from exc
        run = await self.get_run(user_id, project_id, command.run_id)
        selected = list(dict.fromkeys(normalize_keyword(k) for k in command.keywords if k.strip()))
        tags = list(dict.fromkeys(" ".join(tag.split()) for tag in command.tags if tag.strip()))
        if (
            not selected
            or any(len(tag) > 64 for tag in tags)
            or (command.tag_mode == "replace" and not tags)
        ):
            raise _error()
        available = {
            row["keyword"]: row
            for result in reversed(run["results"])
            if result["ok"]
            for row in reversed(result["rows"])
        }
        if any(keyword not in available for keyword in selected):
            raise _error()
        rows = [
            available[keyword]
            | {
                "location_code": run["location_code"],
                "language_code": run["language_code"],
                "observed_at": run["observed_at"],
                "source_run_id": run["run_id"],
            }
            for keyword in selected
        ]
        return await _db(
            self.repo.save_keywords, project_id, rows, tags, command.tag_mode == "replace"
        )

    async def list_keywords(
        self, user_id: int, project_id: str, *, search: str = "", page: int = 1, page_size: int = 50
    ) -> dict[str, Any]:
        if page < 1 or page_size < 1 or page_size > 500:
            raise _error()
        rows = await self.keyword_rows(user_id, project_id, search=search)
        return {"rows": rows[(page - 1) * page_size : page * page_size], "total_count": len(rows)}

    async def keyword_rows(
        self, user_id: int, project_id: str, *, search: str = ""
    ) -> list[dict[str, Any]]:
        await self._project(user_id, project_id)
        return [
            row
            for row in await _db(self.repo.keywords, project_id)
            if search.lower() in row["keyword"]
        ]

    async def report_context(self, user_id: int, project_id: str, run_id: str) -> dict[str, Any]:
        project = await self._project(user_id, project_id)
        run = await self.get_run(user_id, project_id, run_id)
        if run["status"] == "running" or not any(r["ok"] for r in run["results"]):
            raise _error()
        evidence = {
            "project": project,
            "run": run,
            "saved_keywords": await _db(self.repo.keywords, project_id),
        }
        context_hash = hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
        ).hexdigest()
        return evidence | {
            "context_hash": context_hash,
            "instructions": tr("seo.report_instructions", "en"),
        }

    async def save_report(
        self,
        user_id: int,
        project_id: str,
        *,
        run_id: str,
        context_hash: str,
        title: str,
        summary: str,
        html: str,
        skill: str = "keyword-research",
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        html = html.strip()
        try:
            command = ReportCreate(
                run_id=run_id,
                context_hash=context_hash,
                title=title.strip(),
                summary=summary,
                html=html,
                skill=skill,
            )
        except ValidationError as exc:
            raise _error() from exc
        content = command.html.encode("utf-8")
        if (
            len(content) > 500000
            or not re.search(r"<html(?:\s|>)", html, re.I)
            or not html.lower().endswith("</html>")
        ):
            raise _error()
        context = await self.report_context(user_id, project_id, run_id)
        if context["context_hash"] != context_hash:
            raise _error(ErrorCode.SEO_CONFLICT)
        if thread_id is not None:
            thread = await _db(self.repos.thread_repo.get, thread_id)
            if (
                thread is None
                or thread.user_id != user_id
                or thread.agent_id != context["project"]["agent_id"]
            ):
                raise _error(ErrorCode.FORBIDDEN)
        metadata: dict[str, Any] = {
            "report_id": new_ulid(),
            "project_id": project_id,
            "run_id": run_id,
            "title": command.title,
            "summary": summary,
            "skill": skill,
            "created_at": _now(),
            "content_hash": hashlib.sha256(content).hexdigest(),
            "context_hash": context_hash,
            "size_bytes": len(content),
            "thread_id": thread_id,
        }
        if not await _db(self.repo.save_report, metadata, html, context):
            raise _error(ErrorCode.SEO_CONFLICT)
        await self.get_report(user_id, project_id, metadata["report_id"])
        return metadata

    async def list_reports(self, user_id: int, project_id: str) -> list[dict[str, Any]]:
        await self._project(user_id, project_id)
        return await _db(self.repo.reports, project_id)

    async def get_report(self, user_id: int, project_id: str, report_id: str) -> dict[str, Any]:
        await self._project(user_id, project_id)
        report = await _db(self.repo.report, project_id, report_id)
        if report is None:
            raise _error(ErrorCode.NOT_FOUND)
        if hashlib.sha256(report["html"].encode("utf-8")).hexdigest() != report["content_hash"]:
            raise _error(ErrorCode.SEO_REPORT_INTEGRITY)
        return report
