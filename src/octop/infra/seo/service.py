"""Tenant-scoped SEO workflow orchestration; provider output is the metric authority."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
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
    SECTION_KEYS,
    AddCompetitorsOp,
    AddKeyPagesOp,
    AppendResearchLogOp,
    ContextOp,
    ContextUpdate,
    CustomSectionOp,
    DeleteCustomSectionOp,
    KeywordsSave,
    LookupCreate,
    ProjectCreate,
    ProjectUpdate,
    ProviderPut,
    RemoveCompetitorsOp,
    RemoveKeyPagesOp,
    RemoveResearchLogOp,
    ReportCreate,
    ResearchCreate,
    SectionOp,
    SerpCreate,
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


# OpenSEO serves a SERP from its cache for 12 hours (research/serp.ts).
SERP_CACHE_SECONDS = 12 * 60 * 60
# OpenSEO caches domain overviews and suggested keywords for 12 hours too (DomainService.ts),
# buys the top 100 ranked keywords by traffic and lists 50 SERP competitors.
LOOKUP_CACHE_SECONDS = 12 * 60 * 60
DOMAIN_KEYWORDS_LIMIT = 100
COMPETITORS_LIMIT = 50
# Project memory caps from OpenSEO contextUpdateOps.ts / ProjectContextService.ts.
PROSE_MAX_CHARS = 4000
MAX_CUSTOM_SECTIONS = 20
MAX_COMPETITORS = 100
MAX_KEY_PAGES = 100
RESEARCH_LOG_LIMIT = 20
RESEARCH_LOG_RETENTION_DAYS = 90
CUSTOM_PREFIX = "custom:"


def _stale(item: dict[str, Any], minutes: int) -> bool:
    return item["status"] == "running" and datetime.fromisoformat(
        item["observed_at"]
    ) < datetime.now(UTC) - timedelta(minutes=minutes)


def _text(value: str | None) -> str | None:
    return (value.strip() or None) if value is not None else None


def _bare_host(raw: str) -> str:
    """``https://www.Example.com/x`` -> ``example.com``, so one competitor is one row."""
    text = raw.strip()
    parts = urlsplit(text if "://" in text else "https://" + text)
    host = (parts.hostname or "").removeprefix("www.")
    if (
        parts.scheme not in {"http", "https"}
        or parts.username
        or parts.password
        or not re.fullmatch(r"[a-z0-9.-]+", host)
        or "." not in host
    ):
        raise ValueError(f"Not a valid domain: {raw}")
    return host


def _key_page_url(raw: str) -> str:
    """Force https, drop ``www.`` and the fragment, keep path and query (contextUpdateOps.ts)."""
    text = raw.strip()
    parts = urlsplit(text if re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I) else "https://" + text)
    host = (parts.hostname or "").removeprefix("www.")
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not host
        or parts.username
        or parts.password
    ):
        raise ValueError(f"Not a valid page URL: {raw}")
    port = f":{parts.port}" if parts.port and parts.port != 443 else ""
    path = parts.path or "/"
    url = f"https://{host}{port}{path}" + (f"?{parts.query}" if parts.query else "")
    return url[:-1] if path == "/" and not parts.query else url


def _resolve_context(
    updates: list[ContextOp], *, custom: set[str], domains: set[str], urls: set[str]
) -> list[tuple[str, Any]]:
    """Normalize and cap a whole batch before any write, so a rejected op writes nothing."""
    prune_before = (datetime.now(UTC) - timedelta(days=RESEARCH_LOG_RETENTION_DAYS)).date()
    ops: list[tuple[str, Any]] = []

    def prose(content: str) -> str:
        content = content.strip()
        if len(content) > PROSE_MAX_CHARS:
            raise ValueError(f"Sections are capped at {PROSE_MAX_CHARS} characters")
        return content

    def resolve(op: ContextOp) -> None:
        if isinstance(op, SectionOp):
            content = prose(op.content)
            # Empty clears it, so it reads back as missing rather than as an empty section.
            if content:
                ops.append(
                    ("upsert_section", {"key": op.section, "title": None, "content": content})
                )
            else:
                ops.append(("delete_section", op.section))
        elif isinstance(op, CustomSectionOp):
            key = CUSTOM_PREFIX + op.custom_section
            content = prose(op.content)
            if not content:
                ops.append(("delete_section", key))
                custom.discard(key)
                return
            if key not in custom and len(custom) >= MAX_CUSTOM_SECTIONS:
                raise ValueError(f"A project can hold {MAX_CUSTOM_SECTIONS} custom sections")
            ops.append(
                ("upsert_section", {"key": key, "title": _text(op.title), "content": content})
            )
            custom.add(key)
        elif isinstance(op, DeleteCustomSectionOp):
            key = CUSTOM_PREFIX + op.delete_custom_section
            ops.append(("delete_section", key))
            custom.discard(key)
        elif isinstance(op, AddCompetitorsOp):
            # A repeated domain in one op collapses to its last occurrence.
            rows: dict[str, dict[str, Any]] = {}
            for competitor in op.add_competitors:
                domain = _bare_host(competitor.domain)
                rows[domain] = {
                    "domain": domain,
                    "name": _text(competitor.name),
                    "notes": _text(competitor.notes),
                }
            if len(domains | rows.keys()) > MAX_COMPETITORS:
                raise ValueError(f"A project can track {MAX_COMPETITORS} competitors")
            ops.extend(("upsert_competitor", row) for row in rows.values())
            domains.update(rows)
        elif isinstance(op, RemoveCompetitorsOp):
            removed = [_bare_host(domain) for domain in op.remove_competitors]
            ops.extend(("delete_competitor", domain) for domain in removed)
            domains.difference_update(removed)
        elif isinstance(op, AddKeyPagesOp):
            pages: dict[str, dict[str, Any]] = {}
            for page in op.add_key_pages:
                url = _key_page_url(page.url)
                pages[url] = {
                    "url": url,
                    "role": page.role,
                    "topic": _text(page.topic),
                    "notes": _text(page.notes),
                }
            if len(urls | pages.keys()) > MAX_KEY_PAGES:
                raise ValueError(f"A project can hold {MAX_KEY_PAGES} key pages")
            ops.extend(("upsert_key_page", row) for row in pages.values())
            urls.update(pages)
        elif isinstance(op, RemoveKeyPagesOp):
            removed = [_key_page_url(url) for url in op.remove_key_pages]
            ops.extend(("delete_key_page", url) for url in removed)
            urls.difference_update(removed)
        elif isinstance(op, AppendResearchLogOp):
            summary = op.append_research_log.summary.strip()
            if not summary:
                raise ValueError("A research log entry needs a summary")
            ops.append(("append_log", {"summary": summary, "prune_before": str(prune_before)}))
        elif isinstance(op, RemoveResearchLogOp):
            ops.extend(("delete_log", log_id) for log_id in op.remove_research_log)

    for index, op in enumerate(updates):
        try:
            resolve(op)
        except ValueError as exc:
            # The batch is atomic, so name the op that sank it.
            raise OctopError.localized(
                ErrorCode.SEO_INVALID_INPUT,
                message=f"updates[{index}] was rejected (nothing in this batch was applied): {exc}",
            ) from exc
    return ops


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
        # The bound agent only matters to Agent tools: a project whose agent was deleted
        # (agent_id NULL) or shared keeps its evidence readable by its owner.
        return project

    async def project_for_agent(self, user_id: int, agent_id: str) -> dict[str, Any] | None:
        project = next(
            (p for p in await self.list_projects(user_id) if p["agent_id"] == agent_id), None
        )
        if project is None:
            return None
        project = await self._project(user_id, project["project_id"])
        agent = await _db(self.repos.agent_repo.get, agent_id)
        if agent is None or agent.user_id != user_id or agent.kind != "expert":
            raise _error(ErrorCode.FORBIDDEN)
        return project

    async def _bindable_agent(self, user_id: int, agent_id: str) -> None:
        agent = await _db(self.repos.agent_repo.get, agent_id)
        if agent is None or agent.user_id != user_id or agent.is_shared or agent.kind != "expert":
            raise _error(ErrorCode.FORBIDDEN)

    async def bind_agent(
        self, user_id: int, project_id: str, **kwargs: Any
    ) -> tuple[dict[str, Any], str | None]:
        """Bind the project to another owned expert; returns it and the previous agent id."""
        try:
            command = ProjectUpdate.model_validate(kwargs)
        except ValidationError as exc:
            raise _error() from exc
        await self._project(user_id, project_id)
        await self._bindable_agent(user_id, command.agent_id)
        previous = await _db(self.repo.set_agent, user_id, project_id, command.agent_id)
        if previous == "conflict":
            raise _error(ErrorCode.SEO_CONFLICT)
        return await self._project(user_id, project_id), previous or None

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
        await self._bindable_agent(user_id, agent_id)
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

    async def _settle_run(self, run: dict[str, Any]) -> dict[str, Any]:
        # Bounded request has at most 15 calls, each 60s, with auto legs concurrent.
        # Stale rows remain evidence and are never automatically purchased again.
        if _stale(run, 20):
            run.update(status="unknown", error="interrupted")
            await _db(self.repo.update_run, run)
        return run

    async def list_runs(self, user_id: int, project_id: str) -> list[dict[str, Any]]:
        """Run summaries, newest first: per-seed status and row counts, no keyword rows."""
        await self._project(user_id, project_id)
        runs = await _db(self.repo.run_summaries, project_id)
        stale = [run["run_id"] for run in runs if _stale(run, 20)]
        for run_id in stale:
            full = await _db(self.repo.run, project_id, run_id)
            if full is not None:
                await self._settle_run(full)
        if stale:
            runs = await _db(self.repo.run_summaries, project_id)
        for run in runs:
            totals = run.pop("call_costs") or {"calls": 0, "known": None, "missing": 0}
            if not run.get("cached"):
                run["cost_known_usd"] = totals["known"] or 0
                run["cost_usd"] = (
                    totals["known"] if totals["calls"] and not totals["missing"] else None
                )
        return runs

    async def get_run(self, user_id: int, project_id: str, run_id: str) -> dict[str, Any]:
        await self._project(user_id, project_id)
        run = await _db(self.repo.run, project_id, run_id)
        if run is None:
            raise _error(ErrorCode.NOT_FOUND)
        run = await self._settle_run(run)
        run["calls"] = await _db(self.repo.calls, run_id)
        if not run.get("cached"):
            costs = [call["cost_usd"] for call in run["calls"]]
            run["cost_known_usd"] = sum(cost for cost in costs if cost is not None)
            run["cost_usd"] = sum(costs) if costs and all(c is not None for c in costs) else None
        return run

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

    async def _settle_serp(self, serp: dict[str, Any]) -> dict[str, Any]:
        # One live call of at most 60s; a row still running after that was interrupted.
        if _stale(serp, 5):
            serp.update(status="unknown", error="interrupted")
            await _db(self.repo.update_serp, serp)
        return serp

    async def serp(self, user_id: int, project_id: str, **kwargs: Any) -> dict[str, Any]:
        """Buy, or reuse within 12 hours, one Google organic SERP snapshot for the market."""
        project = await self._project(user_id, project_id)
        try:
            command = SerpCreate.model_validate(kwargs)
        except ValidationError as exc:
            raise _error() from exc
        keyword = normalize_keyword(command.keyword)
        if not keyword:
            raise _error()
        market = {
            "keyword": keyword,
            "location_code": project["location_code"],
            "language_code": project["language_code"],
            "device": command.device,
        }

        async def replay() -> dict[str, Any] | None:
            previous = await _db(self.repo.serp_for_key, project_id, command.idempotency_key)
            if previous is None:
                return None
            if (previous["keyword"], previous["device"], previous["requested_depth"]) != (
                keyword,
                command.device,
                command.depth,
            ):
                raise _error(ErrorCode.SEO_CONFLICT)
            return await self._settle_serp(previous)

        if (previous := await replay()) is not None:
            return previous
        # The depth lives in the snapshot, not the cache key: a 100-deep snapshot also
        # answers a 20-deep request. Empty snapshots never answer from cache, so a
        # transient "no results" never hides a good snapshot.
        cached = await _db(
            self.repo.cached_serp,
            project_id,
            market,
            command.depth,
            int(time.time()) - SERP_CACHE_SECONDS,
        )
        client = None if cached else await self._client(user_id)
        serp: dict[str, Any] = market | {
            "serp_id": new_ulid(),
            "project_id": project_id,
            "status": "running",
            "observed_at": _now(),
            "depth": command.depth,
            "requested_depth": command.depth,
            "items": [],
            "cost_usd": None,
            "cached": False,
            "source_serp_id": None,
            "error": None,
            "receipt": {},
        }
        if cached:
            # Cache copies keep the original observation date and depth.
            serp.update(
                status="completed",
                observed_at=cached["observed_at"],
                depth=cached["depth"],
                items=cached["items"],
                cost_usd=0,
                cached=True,
                source_serp_id=cached["serp_id"],
                receipt=cached["receipt"],
            )
        if not await _db(self.repo.start_serp, serp, command.idempotency_key):
            previous = await replay()
            assert previous is not None
            return previous
        if cached:
            return serp
        assert client is not None
        try:
            items, cost, receipt = await client.serp(
                keyword=keyword,
                location_code=market["location_code"],
                language_code=market["language_code"],
                device=command.device,
                depth=command.depth,
            )
            serp.update(status="completed", items=items, cost_usd=cost, receipt=receipt)
        except ProviderFailure as exc:
            serp.update(
                status="unknown" if exc.unknown else "failed",
                cost_usd=exc.cost,
                error=str(exc),
                receipt=exc.receipt,
            )
        except BaseException:
            serp.update(status="unknown", error="interrupted")
            await asyncio.shield(_db(self.repo.update_serp, serp))
            raise
        await _db(self.repo.update_serp, serp)
        return serp

    async def list_serps(
        self, user_id: int, project_id: str, *, keyword: str | None = None
    ) -> list[dict[str, Any]]:
        """Snapshot summaries newest first, without result rows."""
        await self._project(user_id, project_id)
        keyword = normalize_keyword(keyword) if keyword is not None else None
        serps = await _db(self.repo.serps, project_id, keyword)
        stale = [serp["serp_id"] for serp in serps if _stale(serp, 5)]
        for serp_id in stale:
            full = await _db(self.repo.serp, project_id, serp_id)
            if full is not None:
                await self._settle_serp(full)
        return await _db(self.repo.serps, project_id, keyword) if stale else serps

    async def get_serp(self, user_id: int, project_id: str, serp_id: str) -> dict[str, Any]:
        await self._project(user_id, project_id)
        serp = await _db(self.repo.serp, project_id, serp_id)
        if serp is None:
            raise _error(ErrorCode.NOT_FOUND)
        return await self._settle_serp(serp)

    async def _settle_lookup(self, lookup: dict[str, Any]) -> dict[str, Any]:
        # At most two live calls of at most 60s each.
        if _stale(lookup, 5):
            lookup.update(status="unknown", error="interrupted")
            await _db(self.repo.update_lookup, lookup)
        return lookup

    async def lookup(self, user_id: int, project_id: str, **kwargs: Any) -> dict[str, Any]:
        """Buy, or reuse within 12 hours, one competitor-research lookup for the market.

        kind ``domain``: the domain's organic footprint and its top ranked keywords (two
        calls); kind ``competitors``: the domains ranking for a keyword set (one call).
        """
        project = await self._project(user_id, project_id)
        try:
            command = LookupCreate.model_validate(kwargs)
            if command.domain is not None:
                domain: str | None = _bare_host(command.domain)
                keywords: list[str] | None = None
                target_key = domain
            else:
                domain = None
                keywords = sorted({normalize_keyword(k) for k in command.keywords or []} - {""})
                if not keywords:
                    raise ValueError("no keywords")
                target_key = hashlib.sha256("\n".join(keywords).encode()).hexdigest()
        except ValueError as exc:
            raise _error() from exc

        async def replay() -> dict[str, Any] | None:
            previous = await _db(self.repo.lookup_for_key, project_id, command.idempotency_key)
            if previous is None:
                return None
            if (previous["kind"], previous["target_key"]) != (command.kind, target_key):
                raise _error(ErrorCode.SEO_CONFLICT)
            return await self._settle_lookup(previous)

        if (previous := await replay()) is not None:
            return previous
        lookup: dict[str, Any] = {
            "lookup_id": new_ulid(),
            "project_id": project_id,
            "kind": command.kind,
            "target_key": target_key,
            "domain": domain,
            "keywords": keywords,
            "location_code": project["location_code"],
            "language_code": project["language_code"],
            "status": "running",
            "observed_at": _now(),
            "overview": None,
            "total_count": None,
            "items": [],
            "cost_usd": None,
            "cost_known_usd": 0,
            "cached": False,
            "source_lookup_id": None,
            "error": None,
            "receipts": [],
        }
        # Empty lookups never answer from cache, like SERP snapshots.
        cached = await _db(self.repo.cached_lookup, lookup, int(time.time()) - LOOKUP_CACHE_SECONDS)
        client = None if cached else await self._client(user_id)
        if cached:
            lookup.update(
                status="completed",
                observed_at=cached["observed_at"],
                overview=cached["overview"],
                total_count=cached["total_count"],
                items=cached["items"],
                cost_usd=0,
                cached=True,
                source_lookup_id=cached["lookup_id"],
                receipts=cached["receipts"],
            )
        if not await _db(self.repo.start_lookup, lookup, command.idempotency_key):
            previous = await replay()
            assert previous is not None
            return previous
        if cached:
            return lookup
        assert client is not None
        try:
            await self._buy_lookup(client, lookup)
        except BaseException:
            lookup.update(status="unknown", error="interrupted")
            await asyncio.shield(_db(self.repo.update_lookup, lookup))
            raise
        await _db(self.repo.update_lookup, lookup)
        return lookup

    @staticmethod
    async def _buy_lookup(client: DataForSeoClient, lookup: dict[str, Any]) -> None:
        market = {
            "location_code": lookup["location_code"],
            "language_code": lookup["language_code"],
        }
        costs: list[float | None] = []

        def spent(cost: float | None, receipt: dict[str, Any]) -> None:
            costs.append(cost)
            lookup["receipts"].append(receipt)

        try:
            if lookup["kind"] == "domain":
                overview, cost, receipt = await client.domain_overview(
                    target=lookup["domain"], **market
                )
                spent(cost, receipt)
                lookup["overview"] = overview
                ranked, cost, receipt = await client.ranked_keywords(
                    target=lookup["domain"], limit=DOMAIN_KEYWORDS_LIMIT, **market
                )
                spent(cost, receipt)
                lookup.update(total_count=ranked["total_count"], items=ranked["rows"])
            else:
                rows, cost, receipt = await client.serp_competitors(
                    keywords=lookup["keywords"], limit=COMPETITORS_LIMIT, **market
                )
                spent(cost, receipt)
                lookup["items"] = rows
            lookup["status"] = "completed"
        except ProviderFailure as exc:
            spent(exc.cost, exc.receipt)
            lookup.update(status="unknown" if exc.unknown else "failed", error=str(exc))
        finally:
            known = [c for c in costs if c is not None]
            lookup["cost_known_usd"] = sum(known)
            lookup["cost_usd"] = sum(known) if costs and len(known) == len(costs) else None

    async def list_lookups(
        self, user_id: int, project_id: str, *, kind: str | None = None
    ) -> list[dict[str, Any]]:
        """Lookup summaries newest first, without result rows."""
        await self._project(user_id, project_id)
        lookups = await _db(self.repo.lookups, project_id, kind)
        stale = [lookup["lookup_id"] for lookup in lookups if _stale(lookup, 5)]
        for lookup_id in stale:
            full = await _db(self.repo.lookup, project_id, lookup_id)
            if full is not None:
                await self._settle_lookup(full)
        return await _db(self.repo.lookups, project_id, kind) if stale else lookups

    async def get_lookup(self, user_id: int, project_id: str, lookup_id: str) -> dict[str, Any]:
        await self._project(user_id, project_id)
        lookup = await _db(self.repo.lookup, project_id, lookup_id)
        if lookup is None:
            raise _error(ErrorCode.NOT_FOUND)
        return await self._settle_lookup(lookup)

    async def project_context(self, user_id: int, project_id: str) -> dict[str, Any]:
        await self._project(user_id, project_id)
        raw = await _db(self.repo.context, project_id, RESEARCH_LOG_LIMIT)
        stored = {row["section_key"]: row for row in raw["sections"]}
        return {
            # Typed sections keep their declared order; missing ones tell what to fill.
            "sections": [
                {"key": key}
                | {k: v for k, v in stored[key].items() if k not in {"section_key", "title"}}
                for key in SECTION_KEYS
                if key in stored
            ],
            "missing_sections": [key for key in SECTION_KEYS if key not in stored],
            "custom_sections": [
                {"slug": row["section_key"].removeprefix(CUSTOM_PREFIX)}
                | {k: v for k, v in row.items() if k != "section_key"}
                for row in raw["sections"]
                if row["section_key"].startswith(CUSTOM_PREFIX)
            ],
            "competitors": raw["competitors"],
            "key_pages": raw["key_pages"],
            # Newest entries only; the window is the last RESEARCH_LOG_RETENTION_DAYS days.
            "research_log": raw["research_log"],
            "research_log_limit": RESEARCH_LOG_LIMIT,
        }

    async def update_context(
        self, user_id: int, project_id: str, updates: list[Any], *, author: str
    ) -> dict[str, Any]:
        """Apply a batch of context ops atomically; ``author`` is ``user`` or ``agent``."""
        await self._project(user_id, project_id)
        try:
            command = ContextUpdate.model_validate({"updates": updates})
        except ValidationError as exc:
            raise _error() from exc
        raw = await _db(self.repo.context, project_id, 0)
        ops = _resolve_context(
            command.updates,
            custom={
                row["section_key"]
                for row in raw["sections"]
                if row["section_key"].startswith(CUSTOM_PREFIX)
            },
            domains={row["domain"] for row in raw["competitors"]},
            urls={row["url"] for row in raw["key_pages"]},
        )
        await _db(self.repo.apply_context, project_id, ops, author, _now())
        return await self.project_context(user_id, project_id)

    async def report_context(self, user_id: int, project_id: str, run_id: str) -> dict[str, Any]:
        project = await self._project(user_id, project_id)
        run = await self.get_run(user_id, project_id, run_id)
        if run["status"] == "running" or not any(r["ok"] for r in run["results"]):
            raise _error()
        context = await self.project_context(user_id, project_id)
        evidence = {
            "project": project,
            "run": run,
            "saved_keywords": await _db(self.repo.keywords, project_id),
            # The research log is left out: the Agent appends to it while it works,
            # which must not invalidate the report it is writing.
            "project_context": {
                k: v for k, v in context.items() if k not in {"research_log", "research_log_limit"}
            },
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
