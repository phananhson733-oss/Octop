"""Paid lookups the SEO expert proposes in chat and the user buys on its confirm card.

The ``seo_propose_*`` tools sign each purchase with a server key, so the card can only buy what
a proposal tool produced, exactly as proposed. A rank or AI check also carries a fingerprint of
the keywords or prompts it would ask, so the click buys the list the card showed: when that
list or its settings changed after the proposal, the purchase is refused and proposed again.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any

from octop.i18n import tr
from octop.infra.db.services import RepoBundle
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.seo.service import SeoService, _db

_KEY = "seo.proposal_hmac"


def _plain(value: Any) -> Any:
    # The card sends the purchase back through JavaScript, which writes 1.0 as 1.
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(
        _plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _sign(repos: RepoBundle, purchase: dict[str, Any]) -> str:
    key = repos.secret_repo.get_or_create(_KEY, lambda: secrets.token_bytes(32))
    return hmac.new(key, _canonical(purchase), hashlib.sha256).hexdigest()


async def sign(service: SeoService, purchase: dict[str, Any]) -> str:
    return await _db(_sign, service.repos, purchase)


def rank_content(found: dict[str, Any]) -> str:
    """Fingerprint of what a rank check buys, from ``SeoRankService.results``."""
    tracker = found["tracker"]
    keywords = sorted((row["keyword_id"], row["keyword"]) for row in found["rows"])
    settings = [tracker["domain"], tracker["devices"], tracker["depth"]]
    return hashlib.sha256(_canonical([settings, keywords])).hexdigest()


def ai_content(found: dict[str, Any]) -> str:
    """Fingerprint of what an AI visibility check buys, from ``SeoAiService.results``."""
    settings = found["settings"]
    prompts = sorted((prompt["prompt_id"], prompt["prompt"]) for prompt in found["prompts"])
    market = [settings["engines"], settings["location_code"], settings["language_code"]]
    return hashlib.sha256(_canonical([market, prompts])).hexdigest()


def _refused(code: ErrorCode, reason: str, locale: str) -> OctopError:
    text = tr(f"seo.proposal.errors.{reason}", locale)
    return OctopError.localized(code, message=text, details={"reason": text})


async def confirm(
    service: SeoService,
    owner_id: int,
    project_id: str,
    purchase: dict[str, Any],
    signature: str,
    *,
    locale: str = "en",
) -> dict[str, str]:
    """Buy a signed proposal through the same service call as its workbench route.

    The idempotency key is the proposal's, so a second click reads the first attempt.
    """
    from octop.infra.seo.ai_visibility import SeoAiService  # noqa: PLC0415
    from octop.infra.seo.rank import SeoRankService  # noqa: PLC0415

    expected = await sign(service, purchase)
    if not hmac.compare_digest(expected, signature) or purchase.get("project_id") != project_id:
        raise _refused(ErrorCode.FORBIDDEN, "invalid", locale)
    kind = purchase["kind"]
    body = purchase["input"] | {
        "idempotency_key": purchase["idempotency_key"],
        "confirm_paid": True,
    }
    if kind == "research":
        run = await service.research(owner_id, project_id, **body)
        return {"kind": kind, "record_id": run["run_id"], "status": run["status"]}
    if kind == "serp":
        serp = await service.serp(owner_id, project_id, **body)
        return {"kind": kind, "record_id": serp["serp_id"], "status": serp["status"]}
    if kind in ("lookup", "prompt", "brand"):
        buy = {
            "lookup": service.lookup,
            "prompt": service.prompt_lookup,
            "brand": service.brand_lookup,
        }[kind]
        found = await buy(owner_id, project_id, **body)
        return {"kind": kind, "record_id": found["lookup_id"], "status": found["status"]}
    if kind == "rank_check":
        ranks = SeoRankService(service)
        tracker_id = purchase["tracker_id"]
        if rank_content(await ranks.results(owner_id, project_id, tracker_id)) != purchase.get(
            "content_hash"
        ):
            raise _refused(ErrorCode.SEO_CONFLICT, "changed", locale)
        run = await ranks.start_check(owner_id, project_id, tracker_id, locale=locale, **body)
        return {"kind": kind, "record_id": run["run_id"], "status": run["status"]}
    if kind == "ai_check":
        ai = SeoAiService(service)
        if ai_content(await ai.results(owner_id, project_id)) != purchase.get("content_hash"):
            raise _refused(ErrorCode.SEO_CONFLICT, "changed", locale)
        run = await ai.start_check(owner_id, project_id, locale=locale, **body)
        return {"kind": kind, "record_id": run["run_id"], "status": run["status"]}
    raise _refused(ErrorCode.FORBIDDEN, "invalid", locale)
