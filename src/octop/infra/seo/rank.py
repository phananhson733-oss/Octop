"""Rank tracking: where a domain ranks for a keyword list in the project's market.

Ported from OpenSEO rank tracking (RankTrackingService, rankTrackingResults.ts,
RankCheckWorkflow) with manual live checks only. A check buys one live Google SERP per
keyword and device, crawling only until the domain shows, and saves each answer as it
arrives; it runs in the background while the page polls the tracker.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from octop.i18n import tr
from octop.infra.db.repos.seo_rank import SeoRankRepo
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.seo.models import (
    RankCheckCreate,
    RankKeywordsAdd,
    RankTrackerCreate,
    RankTrackerUpdate,
)
from octop.infra.seo.provider import DataForSeoClient, ProviderFailure, normalize_keyword
from octop.infra.seo.service import SeoService, _bare_host, _db, _error, _now
from octop.infra.utils.ulid import new_ulid

logger = logging.getLogger(__name__)

# OpenSEO's live Google Organic prices (shared/rank-tracking.ts): the first page of 10
# results, then each further page.
_FIRST_PAGE_USD = 0.002
_EXTRA_PAGE_USD = 0.0015
# Google Organic bills 5x when a keyword holds an advanced search operator anywhere.
_OPERATOR = re.compile(
    r"(allinanchor|allintext|allintitle|allinurl|cache|define|definition|filetype|id|"
    r"inanchor|info|intext|intitle|inurl|link|site):",
    re.IGNORECASE,
)
COMPARE_DAYS = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}
MAX_TRACKERS = 20
MAX_KEYWORDS = 200
CONCURRENT_CHECKS = 5
# A run beats on every answer, each at most a 60s call; one silent this long was cut off.
STALE_SECONDS = 180
HISTORY_LIMIT = 200
# Checks started by this process, held so the event loop keeps them alive.
_CHECKS: set[asyncio.Task[None]] = set()


def check_cost(keyword: str, depth: int) -> float:
    cost = _FIRST_PAGE_USD + (depth // 10 - 1) * _EXTRA_PAGE_USD
    return cost * 5 if _OPERATOR.search(keyword) else cost


def device_list(devices: str) -> list[str]:
    return ["desktop", "mobile"] if devices == "both" else [devices]


def estimate_usd(keywords: list[str], devices: str, depth: int) -> float:
    """The most a check can cost; stopping at the domain's page often costs less."""
    total = sum(check_cost(keyword, depth) for keyword in keywords)
    return round(total * len(device_list(devices)), 6)


def _rejected(code: ErrorCode, reason: str, locale: str) -> OctopError:
    """The generic code message plus a specific reason the dashboard appends to it."""
    text = tr(f"seo.rank.errors.{reason}", locale)
    return OctopError.localized(code, message=text, details={"reason": text})


def _keyword_rows(keywords: list[str]) -> list[dict[str, Any]]:
    unique = dict.fromkeys(normalize_keyword(keyword) for keyword in keywords)
    now = _now()
    return [
        {"keyword_id": new_ulid(), "keyword": keyword, "created_at": now}
        for keyword in unique
        if keyword
    ]


class SeoRankService:
    def __init__(self, seo: SeoService) -> None:
        self.seo = seo

    @property
    def repo(self) -> SeoRankRepo:
        return self.seo.repos.seo_rank_repo

    async def _tracker(self, user_id: int, project_id: str, tracker_id: str) -> dict[str, Any]:
        await self.seo._project(user_id, project_id)
        tracker = await _db(self.repo.tracker, project_id, tracker_id)
        if tracker is None:
            raise _error(ErrorCode.NOT_FOUND)
        return tracker

    async def _settle(self, tracker_id: str) -> None:
        await _db(self.repo.settle, tracker_id, int(time.time()) - STALE_SECONDS, _now())

    async def list_trackers(self, user_id: int, project_id: str) -> list[dict[str, Any]]:
        await self.seo._project(user_id, project_id)
        return await _db(self.repo.trackers, project_id)

    async def create_tracker(
        self, user_id: int, project_id: str, *, locale: str = "en", **kwargs: Any
    ) -> dict[str, Any]:
        """Track a domain (the project's own by default) in the project's market."""
        project = await self.seo._project(user_id, project_id)
        try:
            command = RankTrackerCreate.model_validate(kwargs)
            domain = _bare_host(command.domain or project["domain"])
        except ValueError as exc:
            raise _error() from exc
        tracker = {
            "tracker_id": new_ulid(),
            "project_id": project_id,
            "domain": domain,
            "location_code": project["location_code"],
            "language_code": project["language_code"],
            "devices": command.devices,
            "depth": command.depth,
            "created_at": _now(),
        }
        status = await _db(
            self.repo.create_tracker, tracker, _keyword_rows(command.keywords), MAX_TRACKERS
        )
        if status != "created":
            raise _rejected(
                ErrorCode.SEO_CONFLICT if status == "exists" else ErrorCode.SEO_INVALID_INPUT,
                status,
                locale,
            )
        return await self.results(user_id, project_id, tracker["tracker_id"])

    async def update_tracker(
        self, user_id: int, project_id: str, tracker_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Change devices or depth; a check already running keeps the settings it started with."""
        tracker = await self._tracker(user_id, project_id, tracker_id)
        try:
            command = RankTrackerUpdate.model_validate(kwargs)
        except ValueError as exc:
            raise _error() from exc
        await _db(
            self.repo.update_tracker,
            tracker_id,
            command.devices or tracker["devices"],
            command.depth or tracker["depth"],
        )
        return await self.results(user_id, project_id, tracker_id)

    async def delete_tracker(
        self, user_id: int, project_id: str, tracker_id: str, *, locale: str = "en"
    ) -> None:
        """Delete a tracker with its history; refused while a check is buying answers."""
        await self._tracker(user_id, project_id, tracker_id)
        await self._settle(tracker_id)
        run = await _db(self.repo.latest_run, tracker_id)
        if run is not None and run["status"] == "running":
            raise _rejected(ErrorCode.SEO_CONFLICT, "busy", locale)
        await _db(self.repo.delete_tracker, tracker_id)

    async def add_keywords(
        self, user_id: int, project_id: str, tracker_id: str, *, locale: str = "en", **kwargs: Any
    ) -> dict[str, int]:
        await self._tracker(user_id, project_id, tracker_id)
        try:
            rows = _keyword_rows(RankKeywordsAdd.model_validate(kwargs).keywords)
        except ValueError as exc:
            raise _error() from exc
        if not rows:
            raise _error()
        added = await _db(self.repo.add_keywords, tracker_id, rows, MAX_KEYWORDS)
        if added is None:
            raise _rejected(ErrorCode.SEO_INVALID_INPUT, "too_many_keywords", locale)
        return {"added": added}

    async def remove_keyword(
        self, user_id: int, project_id: str, tracker_id: str, keyword_id: str
    ) -> None:
        """Stop tracking a keyword. Its snapshots stay readable through its history."""
        await self._tracker(user_id, project_id, tracker_id)
        if not await _db(self.repo.remove_keyword, tracker_id, keyword_id):
            raise _error(ErrorCode.NOT_FOUND)

    async def results(
        self, user_id: int, project_id: str, tracker_id: str, *, compare: str = "7d"
    ) -> dict[str, Any]:
        """Each keyword's newest position per device against an earlier one.

        The earlier position is the newest snapshot older than the compare period, or the
        first snapshot when there is none that old (rankTrackingResults.ts).
        """
        tracker = await self._tracker(user_id, project_id, tracker_id)
        if compare not in COMPARE_DAYS:
            raise _error()
        await self._settle(tracker_id)
        keywords = await _db(self.repo.keywords, tracker_id)
        before = (datetime.now(UTC) - timedelta(days=COMPARE_DAYS[compare])).isoformat()
        latest, previous, earliest = await _db(self.repo.positions, tracker_id, before)
        baseline = {(s["keyword_id"], s["device"]): s for s in earliest}
        baseline |= {(s["keyword_id"], s["device"]): s for s in previous}
        current = {(s["keyword_id"], s["device"]): s for s in latest}

        def cell(keyword_id: str, device: str) -> dict[str, Any]:
            now, then = current.get((keyword_id, device)), baseline.get((keyword_id, device))
            return {
                "position": now["position"] if now else None,
                "url": now["url"] if now else None,
                "serp_features": now["serp_features"] if now else [],
                "checked_at": now["checked_at"] if now else None,
                "previous_position": then["position"] if then else None,
                "previous_checked_at": then["checked_at"] if then else None,
            }

        estimate = estimate_usd(
            [keyword["keyword"] for keyword in keywords], tracker["devices"], tracker["depth"]
        )
        return {
            "tracker": tracker
            | {
                "keyword_count": len(keywords),
                "estimate_usd": estimate,
                # From the snapshots, so a newer failed run keeps the date of what is shown.
                "last_checked_at": max((s["checked_at"] for s in latest), default=None),
            },
            "run": await _db(self.repo.latest_run, tracker_id),
            "compare": compare,
            "rows": [
                {
                    "keyword_id": keyword["keyword_id"],
                    "keyword": keyword["keyword"],
                    "desktop": cell(keyword["keyword_id"], "desktop"),
                    "mobile": cell(keyword["keyword_id"], "mobile"),
                }
                for keyword in keywords
            ],
        }

    async def history(
        self, user_id: int, project_id: str, tracker_id: str, keyword_id: str
    ) -> list[dict[str, Any]]:
        """A keyword's snapshots oldest first, kept after the keyword is removed."""
        await self._tracker(user_id, project_id, tracker_id)
        return await _db(self.repo.history, tracker_id, keyword_id, HISTORY_LIMIT)

    async def start_check(
        self, user_id: int, project_id: str, tracker_id: str, *, locale: str = "en", **kwargs: Any
    ) -> dict[str, Any]:
        """Start a paid check of every keyword; the same key returns the same run.

        Refused when the estimate is above ``max_cost_usd``, the amount the admin approved.
        """
        tracker = await self._tracker(user_id, project_id, tracker_id)
        try:
            command = RankCheckCreate.model_validate(kwargs)
        except ValueError as exc:
            raise _error() from exc
        keywords = await _db(self.repo.keywords, tracker_id)
        if not keywords:
            raise _rejected(ErrorCode.SEO_INVALID_INPUT, "no_keywords", locale)
        devices = device_list(tracker["devices"])
        estimate = estimate_usd(
            [keyword["keyword"] for keyword in keywords], tracker["devices"], tracker["depth"]
        )
        if estimate > command.max_cost_usd + 1e-9:
            raise _rejected(ErrorCode.SEO_CONFLICT, "estimate_changed", locale)
        client = await self.seo._client(user_id)
        run: dict[str, Any] = {
            "run_id": new_ulid(),
            "tracker_id": tracker_id,
            "idempotency_key": command.idempotency_key,
            "status": "running",
            "started_at": _now(),
            "heartbeat_at": int(time.time()),
            "devices": tracker["devices"],
            "depth": tracker["depth"],
            "keywords_total": len(keywords),
            "checks_total": len(keywords) * len(devices),
            "checks_done": 0,
            "checks_failed": 0,
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
            self._run_check(client, tracker, run, keywords), name=f"seo-rank-{run['run_id']}"
        )
        _CHECKS.add(task)
        task.add_done_callback(_CHECKS.discard)
        return dict(run)

    async def _run_check(
        self,
        client: DataForSeoClient,
        tracker: dict[str, Any],
        run: dict[str, Any],
        keywords: list[dict[str, Any]],
    ) -> None:
        slots = asyncio.Semaphore(CONCURRENT_CHECKS)
        # One writer at a time, so the stored progress never goes backwards.
        writing = asyncio.Lock()
        costs: list[float | None] = []
        held = True

        async def save(snapshot: dict[str, Any] | None = None) -> None:
            nonlocal held
            async with writing:
                known = [cost for cost in costs if cost is not None]
                run.update(
                    heartbeat_at=int(time.time()),
                    cost_known_usd=round(sum(known), 6),
                    cost_usd=round(sum(known), 6) if len(known) == len(costs) else None,
                )
                # The run was settled as interrupted elsewhere: buy nothing more.
                if not await _db(self.repo.save_run, dict(run), snapshot):
                    held = False

        async def check(keyword: dict[str, Any], device: str) -> None:
            async with slots:
                if not held:
                    return
                try:
                    found, cost, _ = await client.rank_check(
                        keyword=keyword["keyword"],
                        target=tracker["domain"],
                        location_code=tracker["location_code"],
                        language_code=tracker["language_code"],
                        device=device,
                        depth=run["depth"],
                    )
                except ProviderFailure as exc:
                    costs.append(exc.cost)
                    run["checks_done"] += 1
                    run["checks_failed"] += 1
                    run["error"] = str(exc)
                    await save()
                    return
                costs.append(cost)
                run["checks_done"] += 1
                await save(
                    found
                    | {
                        "run_id": run["run_id"],
                        "keyword_id": keyword["keyword_id"],
                        "keyword": keyword["keyword"],
                        "device": device,
                        "cost_usd": cost,
                        "checked_at": _now(),
                    }
                )

        async def interrupt() -> None:
            # A check in flight may have been charged without an answer.
            run.update(status="unknown", error="interrupted", completed_at=_now())
            costs.append(None)
            try:
                await asyncio.shield(save())
            except Exception:
                logger.warning("SEO rank check %s could not be closed", run["run_id"])

        try:
            async with asyncio.TaskGroup() as group:
                for keyword in keywords:
                    for device in device_list(run["devices"]):
                        group.create_task(check(keyword, device))
            if held:
                failed = run["checks_failed"] == run["checks_total"]
                run.update(status="failed" if failed else "completed", completed_at=_now())
                await save()
        except Exception:
            logger.warning("SEO rank check %s failed", run["run_id"], exc_info=True)
            await interrupt()
        except BaseException:
            # Shutdown.
            await interrupt()
            raise
