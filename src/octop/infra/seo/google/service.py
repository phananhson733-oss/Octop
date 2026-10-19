"""Admin Google grants and per-project Search Console / Analytics properties for SEO.

Ported from OpenSEO d7ea785 ``GscService.ts``, ``googleOAuth.ts`` and
``serverFunctions/searchPerformance.ts``. Grants are per admin and encrypted like the
DataForSEO credentials; a project binds one verified property of one grant.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from cryptography.fernet import InvalidToken
from pydantic import ValidationError

from octop.infra.connectors.crypto import decrypt_credentials, encrypt_credentials
from octop.infra.connectors.oauth.pkce import new_pkce_pair
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.seo.google import ga4, gsc, oauth
from octop.infra.seo.models import (
    Ga4Bind,
    Ga4Overview,
    Ga4Report,
    GscBind,
    GscInspect,
    GscPerformance,
    GscReportQuery,
    GscTableQuery,
)
from octop.infra.seo.service import SeoService
from octop.infra.users.identity import Role
from octop.infra.utils.ulid import new_ulid

if TYPE_CHECKING:
    from octop.infra.db.repos.seo_google import SeoGoogleRepo
    from octop.infra.db.services import RepoBundle

DEFAULT_RETURN_PATH = "/workbench"
# Query string keys the dashboard reads after Google sends the admin back.
RESULT_PARAM = "seo_google"
ERROR_PARAM = "seo_google_error"
# query x page fans out over pages, so it needs more rows to find the 5-20 band.
_STRIKING_FETCH_ROWS = 1000
# One row per day; the longest page range (16 months) is about 490 days.
_DAILY_ROWS = 500
_COUNTRY_ROWS = 25


async def _db[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    return await asyncio.get_running_loop().run_in_executor(None, partial(fn, *args, **kwargs))


def _error(code: ErrorCode, **details: Any) -> OctopError:
    return OctopError.localized(code, details=details, **details)


def _with_params(path: str, **params: str) -> str:
    parts = urlsplit(path)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in {RESULT_PARAM, ERROR_PARAM}]
    return urlunsplit(("", "", parts.path, urlencode(query + list(params.items())), parts.fragment))


def _account(grant: dict[str, Any]) -> dict[str, Any]:
    return {
        "grant_id": grant["grant_id"],
        "email": grant["email"],
        "search_console": oauth.GSC_SCOPE in grant["scopes"],
        "analytics": oauth.GA4_SCOPE in grant["scopes"],
        "updated_at": grant["updated_at"],
    }


def _message(exc: gsc.GoogleApiError) -> str:
    """Google's own error message, or the start of the body when it has none."""
    try:
        message = json.loads(exc.body).get("error", {}).get("message")
    except (ValueError, AttributeError):
        message = None
    return str(message or exc.body[:200])


def _google_failure(exc: gsc.GoogleApiError) -> OctopError:
    if exc.denied:
        return _error(ErrorCode.SEO_GOOGLE_RECONNECT)
    detail = "SERVICE_DISABLED" if exc.api_disabled else _message(exc)[:300]
    return _error(ErrorCode.SEO_GOOGLE_API, google_status=exc.status, detail=detail)


def _ga4_failure(exc: Exception) -> OctopError:
    """Analytics errors. Unlike Search Console, a 403 means the account lost access to the
    property (or the API is off), which a new consent does not fix, so only 401 asks for it."""
    if isinstance(exc, ga4.Ga4Malformed):
        return _error(ErrorCode.SEO_GOOGLE_API, google_status=0, detail="malformed report")
    if isinstance(exc, httpx.HTTPError):
        return _error(ErrorCode.SEO_GOOGLE_API, google_status=0, detail=type(exc).__name__)
    assert isinstance(exc, gsc.GoogleApiError)
    if exc.status == 401:
        return _error(ErrorCode.SEO_GOOGLE_RECONNECT)
    detail = "SERVICE_DISABLED" if exc.api_disabled else _message(exc)[:300]
    return _error(ErrorCode.SEO_GOOGLE_API, google_status=exc.status, detail=detail)


_GA4_ERRORS = (gsc.GoogleApiError, ga4.Ga4Malformed, httpx.HTTPError)


def _rounded(row: dict[str, Any]) -> dict[str, Any]:
    # Google returns full-precision floats; four decimals of CTR and one of position
    # is all anyone reads.
    out = {
        "keys": row.get("keys") or [],
        "clicks": row.get("clicks", 0),
        "impressions": row.get("impressions", 0),
        "ctr": round(row.get("ctr", 0), 4),
    }
    if row.get("position") is not None:
        out["position"] = round(row["position"], 1)
    return out


class SeoGoogleService:
    def __init__(
        self,
        seo: SeoService,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.seo = seo
        self._transport = transport
        self._clock = clock

    @property
    def repos(self) -> RepoBundle:
        return self.seo.repos

    @property
    def repo(self) -> SeoGoogleRepo:
        return self.repos.seo_google_repo

    def _today(self) -> date:
        return datetime.fromtimestamp(self._clock(), UTC).date()

    async def _admin(self, user_id: int) -> None:
        user = await _db(self.repos.user_repo.get, user_id)
        if user is None or user.role != Role.ADMIN:
            raise _error(ErrorCode.FORBIDDEN)

    @staticmethod
    def _config() -> tuple[str, str]:
        config = oauth.client_config()
        if config is None:
            raise _error(ErrorCode.SEO_GOOGLE_NOT_CONFIGURED)
        return config

    # -- Google accounts -------------------------------------------------------------

    async def status(self, user_id: int, base_url: str) -> dict[str, Any]:
        await self._admin(user_id)
        grants = await _db(self.repo.grants, user_id)
        return {
            "configured": oauth.client_config() is not None,
            "redirect_uri": base_url + oauth.CALLBACK_PATH,
            "accounts": [_account(grant) for grant in grants],
        }

    async def start(self, user_id: int, base_url: str, return_path: str | None) -> dict[str, str]:
        await self._admin(user_id)
        client_id, _secret = self._config()
        state = secrets.token_urlsafe(32)
        verifier, challenge = new_pkce_pair()
        redirect_uri = base_url + oauth.CALLBACK_PATH
        now = int(self._clock())
        await _db(
            self.repo.create_state,
            {
                "state": state,
                "user_id": user_id,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "return_path": oauth.safe_return_path(return_path, DEFAULT_RETURN_PATH),
                "created_at": now,
            },
            now - oauth.STATE_TTL_S,
        )
        url = oauth.authorize_url(
            client_id=client_id, redirect_uri=redirect_uri, state=state, code_challenge=challenge
        )
        return {"authorize_url": url}

    async def finish(self, *, state: str | None, code: str | None, error: str | None) -> str:
        """Handle Google's redirect and return where to send the browser.

        The state is consumed first, so one presented with an error is burned rather
        than left for a second try.
        """
        row = await _db(self.repo.consume_state, state) if state else None
        if row is None or row["created_at"] < self._clock() - oauth.STATE_TTL_S:
            return _with_params(DEFAULT_RETURN_PATH, **{ERROR_PARAM: "invalid_state"})
        back = row["return_path"]

        def fail(reason: str) -> str:
            return _with_params(back, **{ERROR_PARAM: reason})

        user = await _db(self.repos.user_repo.get, row["user_id"])
        if user is None or user.role != Role.ADMIN:
            return fail("forbidden")
        if error:
            return fail("access_denied" if error == "access_denied" else "google_error")
        config = oauth.client_config()
        if not code or config is None:
            return fail("missing_code" if config else "not_configured")
        client_id, client_secret = config
        now = self._clock()
        try:
            tokens = await oauth.fetch_tokens(
                {
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": row["redirect_uri"],
                    "grant_type": "authorization_code",
                    "code_verifier": row["code_verifier"],
                },
                transport=self._transport,
            )
            account_id, email = oauth.google_account(
                str(tokens.get("id_token") or ""), client_id, now
            )
        except (oauth.GoogleRejected, httpx.HTTPError):
            return fail("code_rejected")
        scopes = oauth.granted_scopes(tokens)
        # Google lets the admin approve sign-in with both data boxes unchecked; such a
        # grant gets 403 on every call, so send them back to check a box instead.
        if oauth.GSC_SCOPE not in scopes and oauth.GA4_SCOPE not in scopes:
            return fail("scope_not_granted")
        previous = next(
            (
                g
                for g in await _db(self.repo.grants, row["user_id"])
                if g["google_account_id"] == account_id
            ),
            None,
        )
        previous_refresh = None
        if previous is not None:
            try:
                previous_refresh = (await self._credentials(previous)).get("refresh_token")
            except OctopError:
                previous_refresh = None
        stored = oauth.credentials(tokens, now, previous_refresh)
        encrypted = await _db(encrypt_credentials, self.repos.secret_repo, stored)
        await _db(
            self.repo.save_grant,
            {
                "grant_id": new_ulid(),
                "user_id": row["user_id"],
                "google_account_id": account_id,
                "email": email,
                "scopes": scopes,
                "credentials": encrypted.decode("ascii"),
                "created_at": int(now),
                "updated_at": int(now),
            },
        )
        return _with_params(back, **{RESULT_PARAM: "connected"})

    async def delete_account(self, user_id: int, grant_id: str) -> None:
        await self._admin(user_id)
        if not await _db(self.repo.delete_grant, user_id, grant_id):
            raise _error(ErrorCode.NOT_FOUND)

    async def _credentials(self, grant: dict[str, Any]) -> dict[str, Any]:
        try:
            return await _db(
                decrypt_credentials, self.repos.secret_repo, grant["credentials"].encode("ascii")
            )
        except (InvalidToken, ValueError) as exc:
            raise _error(ErrorCode.SEO_GOOGLE_RECONNECT) from exc

    async def _access_token(self, grant: dict[str, Any]) -> str:
        """A valid access token, refreshed and stored again when it is about to expire."""
        stored = await self._credentials(grant)
        now = self._clock()
        if not oauth.needs_refresh(stored, now):
            return str(stored["access_token"])
        refresh = stored.get("refresh_token")
        config = oauth.client_config()
        if config is None:
            raise _error(ErrorCode.SEO_GOOGLE_NOT_CONFIGURED)
        if not refresh:
            # Google issues a refresh token only on a consent; without one the grant
            # cannot recover once its access token expires.
            raise _error(ErrorCode.SEO_GOOGLE_RECONNECT)
        try:
            tokens = await oauth.fetch_tokens(
                {
                    "client_id": config[0],
                    "client_secret": config[1],
                    "refresh_token": str(refresh),
                    "grant_type": "refresh_token",
                },
                transport=self._transport,
            )
        except oauth.GoogleRejected as exc:
            raise _error(ErrorCode.SEO_GOOGLE_RECONNECT) from exc
        except httpx.HTTPError as exc:
            raise _error(
                ErrorCode.SEO_GOOGLE_API, google_status=0, detail=type(exc).__name__
            ) from exc
        fresh = oauth.credentials(tokens, now, str(refresh))
        encrypted = await _db(encrypt_credentials, self.repos.secret_repo, fresh)
        await _db(
            self.repo.update_credentials, grant["grant_id"], encrypted.decode("ascii"), int(now)
        )
        return str(fresh["access_token"])

    def _token(self, grant: dict[str, Any]) -> Callable[[], Awaitable[str]]:
        # A client serves one request; its parallel calls share one token, so an
        # expired grant is refreshed once rather than once per call.
        pending: asyncio.Future[str] | None = None

        async def token() -> str:
            nonlocal pending
            if pending is None:
                pending = asyncio.ensure_future(self._access_token(grant))
            return await asyncio.shield(pending)

        return token

    def _client(self, grant: dict[str, Any]) -> gsc.GscClient:
        return gsc.GscClient(self._token(grant), transport=self._transport)

    def _ga4_client(self, grant: dict[str, Any]) -> ga4.Ga4Client:
        return ga4.Ga4Client(self._token(grant), transport=self._transport)

    # -- Search Console --------------------------------------------------------------

    async def gsc_connection(self, user_id: int, project_id: str) -> dict[str, Any] | None:
        await self.seo._project(user_id, project_id)
        return await _db(self.repo.gsc, project_id)

    async def gsc_sites(self, user_id: int, project_id: str) -> dict[str, Any]:
        """Each grant's properties, or why they could not be listed."""
        await self.seo._project(user_id, project_id)
        grants = await _db(self.repo.grants, user_id)

        async def listed(grant: dict[str, Any]) -> dict[str, Any]:
            out = _account(grant) | {
                "sites": [],
                "requires_reconnect": False,
                "api_disabled": False,
            }
            if oauth.GSC_SCOPE not in grant["scopes"]:
                return out | {"requires_reconnect": True}
            try:
                sites = await self._client(grant).list_sites()
            except gsc.GoogleApiError as exc:
                return out | {"requires_reconnect": exc.denied, "api_disabled": exc.api_disabled}
            except OctopError as exc:
                if exc.code != ErrorCode.SEO_GOOGLE_RECONNECT:
                    raise
                return out | {"requires_reconnect": True}
            return out | {
                "sites": [
                    {"site_url": s.get("siteUrl"), "permission_level": s.get("permissionLevel")}
                    for s in sites
                    if s.get("siteUrl")
                ]
            }

        return {"accounts": list(await asyncio.gather(*(listed(g) for g in grants)))}

    async def gsc_bind(self, user_id: int, project_id: str, **kwargs: Any) -> dict[str, Any]:
        """Bind a verified property that the grant really has."""
        await self.seo._project(user_id, project_id)
        try:
            command = GscBind.model_validate(kwargs)
        except ValidationError as exc:
            raise _error(ErrorCode.SEO_INVALID_INPUT) from exc
        grant = await _db(self.repo.grant, user_id, command.grant_id)
        if grant is None:
            raise _error(ErrorCode.NOT_FOUND)
        try:
            sites = await self._client(grant).list_sites()
        except gsc.GoogleApiError as exc:
            raise _google_failure(exc) from exc
        match = next((s for s in sites if s.get("siteUrl") == command.site_url), None)
        if match is None:
            raise _error(ErrorCode.NOT_FOUND)
        if match.get("permissionLevel") == gsc.UNVERIFIED_PERMISSION:
            raise _error(ErrorCode.FORBIDDEN)
        await _db(
            self.repo.set_gsc,
            {
                "project_id": project_id,
                "grant_id": grant["grant_id"],
                "site_url": command.site_url,
                "permission_level": str(match.get("permissionLevel") or ""),
                "connected_at": int(self._clock()),
            },
        )
        connection = await _db(self.repo.gsc, project_id)
        assert connection is not None
        return connection

    async def gsc_unbind(self, user_id: int, project_id: str) -> None:
        await self.seo._project(user_id, project_id)
        await _db(self.repo.delete_gsc, project_id)

    async def _bound(self, user_id: int, project_id: str) -> tuple[dict[str, Any], gsc.GscClient]:
        await self.seo._project(user_id, project_id)
        connection = await _db(self.repo.gsc, project_id)
        if connection is None:
            raise _error(ErrorCode.SEO_GOOGLE_NOT_CONNECTED)
        grant = await _db(self.repo.grant, user_id, connection["grant_id"])
        if grant is None:
            raise _error(ErrorCode.SEO_GOOGLE_NOT_CONNECTED)
        return connection, self._client(grant)

    async def _query(
        self, client: gsc.GscClient, site_url: str, body: dict[str, Any]
    ) -> list[dict[str, Any]]:
        try:
            return await client.query(site_url, body)
        except gsc.GoogleApiError as exc:
            raise _google_failure(exc) from exc
        except httpx.HTTPError as exc:
            raise _error(
                ErrorCode.SEO_GOOGLE_API, google_status=0, detail=type(exc).__name__
            ) from exc

    async def gsc_performance(self, user_id: int, project_id: str, **kwargs: Any) -> dict[str, Any]:
        """Search Analytics rows for the expert, with position/impression filters.

        Google sorts by clicks and cannot filter by position, so with a metric filter
        the top 1000 rows of the window are fetched and filtered here. Paging stays in
        Google's row space: ``next_start_row`` follows the last row returned.
        """
        try:
            command = GscPerformance.model_validate(kwargs)
        except ValidationError as exc:
            raise _error(ErrorCode.SEO_INVALID_INPUT) from exc
        connection, client = await self._bound(user_id, project_id)
        metric = (command.min_position, command.max_position, command.min_impressions)
        filtered = any(value is not None for value in metric)
        fetch_limit = gsc.MAX_ROW_LIMIT if filtered else command.row_limit
        body = gsc.build_request(
            today=self._today(),
            dimensions=list(command.dimensions) if command.dimensions else None,
            date_range=command.date_range,
            start_date=command.start_date,
            end_date=command.end_date,
            filters=[f.model_dump() for f in command.filters] if command.filters else None,
            row_limit=fetch_limit,
            start_row=command.start_row,
            search_type=command.search_type,
            data_state=command.data_state,
        )
        fetched = await self._query(client, connection["site_url"], body)

        def keep(row: dict[str, Any]) -> bool:
            position = row.get("position")
            if command.min_impressions is not None and row["impressions"] < command.min_impressions:
                return False
            if command.min_position is not None and (
                position is None or position < command.min_position
            ):
                return False
            return not (
                command.max_position is not None
                and (position is None or position > command.max_position)
            )

        kept = [(index, row) for index, row in enumerate(fetched) if not filtered or keep(row)]
        return {
            "site_url": connection["site_url"],
            "request": body,
            "fetched": len(fetched),
            "fetch_limit": fetch_limit,
            "filtered": filtered,
            "rows": [(index, _rounded(row)) for index, row in kept[: command.row_limit]],
            "kept": len(kept),
        }

    def _filters(
        self, command: GscReportQuery
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        """Filters for every call, and the same without country for the country list."""
        base: list[dict[str, str]] = []
        if command.device:
            base.append({"dimension": "device", "operator": "equals", "expression": command.device})
        for dimension, text in (("page", command.page_filter), ("query", command.query_filter)):
            if text is not None:
                base.append(
                    {
                        "dimension": dimension,
                        "operator": text.operator,
                        "expression": text.expression,
                    }
                )
        with_country = base + (
            [{"dimension": "country", "operator": "equals", "expression": command.country}]
            if command.country
            else []
        )
        return with_country, base

    async def gsc_report(self, user_id: int, project_id: str, **kwargs: Any) -> dict[str, Any]:
        """Current and previous totals, the daily series, striking distance and countries."""
        try:
            command = GscReportQuery.model_validate(kwargs)
        except ValidationError as exc:
            raise _error(ErrorCode.SEO_INVALID_INPUT) from exc
        connection, client = await self._bound(user_id, project_id)
        today = self._today()
        start, end = gsc.resolve_date_range(date_range=command.date_range, today=today)
        prev_start, prev_end = gsc.previous_period(start, end)
        filters, no_country = self._filters(command)

        def body(dimensions: list[str], start_date: str, end_date: str, rows: int, f: Any) -> Any:
            return gsc.build_request(
                today=today,
                dimensions=dimensions,
                start_date=start_date,
                end_date=end_date,
                filters=f,
                row_limit=rows,
            )

        site = connection["site_url"]
        current, previous, query_pages, countries = await asyncio.gather(
            self._query(client, site, body(["date"], start, end, _DAILY_ROWS, filters)),
            self._query(client, site, body(["date"], prev_start, prev_end, _DAILY_ROWS, filters)),
            self._query(
                client, site, body(["query", "page"], start, end, _STRIKING_FETCH_ROWS, filters)
            ),
            self._query(client, site, body(["country"], start, end, _COUNTRY_ROWS, no_country)),
        )
        return {
            "site_url": site,
            "email": connection["email"],
            "range": {
                "start_date": start,
                "end_date": end,
                "prev_start_date": prev_start,
                "prev_end_date": prev_end,
            },
            "totals": gsc.sum_totals(current),
            "prev_totals": gsc.sum_totals(previous),
            "daily": [
                {"date": row["keys"][0]} | {k: row.get(k) for k in ("clicks", "impressions")}
                for row in current
                if row.get("keys")
            ],
            "striking_distance": gsc.striking_distance(query_pages),
            "countries": gsc.dimension_rows(countries),
        }

    async def gsc_table(self, user_id: int, project_id: str, **kwargs: Any) -> dict[str, Any]:
        """One page of the queries or pages table; Google gives no total, so one extra
        row tells whether another page exists."""
        try:
            command = GscTableQuery.model_validate(kwargs)
        except ValidationError as exc:
            raise _error(ErrorCode.SEO_INVALID_INPUT) from exc
        connection, client = await self._bound(user_id, project_id)
        today = self._today()
        start, end = gsc.resolve_date_range(date_range=command.date_range, today=today)
        filters, _ = self._filters(command)
        offset = (command.page - 1) * command.page_size
        fetched = gsc.dimension_rows(
            await self._query(
                client,
                connection["site_url"],
                gsc.build_request(
                    today=today,
                    dimensions=[command.dimension],
                    start_date=start,
                    end_date=end,
                    filters=filters,
                    row_limit=command.page_size + 1,
                    start_row=offset,
                ),
            )
        )
        has_next = len(fetched) > command.page_size
        rows = fetched[: command.page_size]
        return {
            "dimension": command.dimension,
            "page": command.page,
            "page_size": command.page_size,
            "has_next_page": has_next,
            "total_count": offset + len(rows) if not has_next and (offset == 0 or rows) else None,
            "rows": rows,
        }

    async def gsc_inspect(self, user_id: int, project_id: str, **kwargs: Any) -> dict[str, Any]:
        """Google's stored index status for up to 10 URLs; one bad URL does not fail the rest."""
        try:
            command = GscInspect.model_validate(kwargs)
        except ValidationError as exc:
            raise _error(ErrorCode.SEO_INVALID_INPUT) from exc
        connection, client = await self._bound(user_id, project_id)
        results: list[dict[str, Any]] = []
        for url in command.urls:
            # Google answers 403 for a URL outside the property too, so API errors stay
            # per URL; a grant that cannot mint a token still fails the whole call.
            try:
                result = await client.inspect(connection["site_url"], url, command.language_code)
            except gsc.GoogleApiError as exc:
                results.append({"url": url, "result": None, "error": _detail(exc)})
                continue
            except httpx.HTTPError as exc:
                results.append({"url": url, "result": None, "error": type(exc).__name__})
                continue
            results.append({"url": url, "result": result})
        return {"site_url": connection["site_url"], "results": results}

    # -- Analytics -------------------------------------------------------------------

    async def ga4_connection(self, user_id: int, project_id: str) -> dict[str, Any] | None:
        await self.seo._project(user_id, project_id)
        return await _db(self.repo.ga4, project_id)

    async def ga4_properties(self, user_id: int, project_id: str) -> dict[str, Any]:
        """Each grant's Analytics properties, or why they could not be listed."""
        await self.seo._project(user_id, project_id)
        grants = await _db(self.repo.grants, user_id)

        async def listed(grant: dict[str, Any]) -> dict[str, Any]:
            out = _account(grant) | {
                "properties": [],
                "requires_reconnect": False,
                "api_disabled": False,
            }
            if oauth.GA4_SCOPE not in grant["scopes"]:
                return out | {"requires_reconnect": True}
            try:
                properties = await self._ga4_client(grant).list_properties()
            except gsc.GoogleApiError as exc:
                return out | {
                    "requires_reconnect": exc.status == 401,
                    "api_disabled": exc.api_disabled,
                }
            except OctopError as exc:
                if exc.code != ErrorCode.SEO_GOOGLE_RECONNECT:
                    raise
                return out | {"requires_reconnect": True}
            except _GA4_ERRORS as exc:
                raise _ga4_failure(exc) from exc
            return out | {"properties": properties}

        return {"accounts": list(await asyncio.gather(*(listed(g) for g in grants)))}

    async def ga4_bind(self, user_id: int, project_id: str, **kwargs: Any) -> dict[str, Any]:
        """Bind a property the grant can read; its time zone and currency are kept."""
        await self.seo._project(user_id, project_id)
        try:
            command = Ga4Bind.model_validate(kwargs)
        except ValidationError as exc:
            raise _error(ErrorCode.SEO_INVALID_INPUT) from exc
        grant = await _db(self.repo.grant, user_id, command.grant_id)
        if grant is None:
            raise _error(ErrorCode.NOT_FOUND)
        client = self._ga4_client(grant)
        try:
            listed = await client.list_properties()
            if not any(p["property_id"] == command.property_id for p in listed):
                raise _error(ErrorCode.NOT_FOUND)
            prop = await client.get_property(command.property_id)
        except _GA4_ERRORS as exc:
            raise _ga4_failure(exc) from exc
        await _db(
            self.repo.set_ga4,
            prop
            | {
                "project_id": project_id,
                "grant_id": grant["grant_id"],
                "connected_at": int(self._clock()),
            },
        )
        connection = await _db(self.repo.ga4, project_id)
        assert connection is not None
        return connection

    async def ga4_unbind(self, user_id: int, project_id: str) -> None:
        await self.seo._project(user_id, project_id)
        await _db(self.repo.delete_ga4, project_id)

    async def _ga4_bound(
        self, user_id: int, project_id: str
    ) -> tuple[dict[str, Any], ga4.Ga4Client]:
        await self.seo._project(user_id, project_id)
        connection = await _db(self.repo.ga4, project_id)
        if connection is None:
            raise _error(ErrorCode.SEO_GOOGLE_NOT_CONNECTED)
        grant = await _db(self.repo.grant, user_id, connection["grant_id"])
        if grant is None:
            raise _error(ErrorCode.SEO_GOOGLE_NOT_CONNECTED)
        return connection, self._ga4_client(grant)

    def _ga4_dates(
        self, connection: dict[str, Any], start_date: str | None, end_date: str | None
    ) -> dict[str, Any]:
        try:
            return ga4.resolve_date_range(
                start_date=start_date,
                end_date=end_date,
                time_zone=connection["time_zone"],
                now=self._clock(),
            )
        except ga4.Ga4Invalid as exc:
            raise OctopError.localized(ErrorCode.SEO_INVALID_INPUT, message=str(exc)) from exc

    @staticmethod
    def _ga4_source(connection: dict[str, Any]) -> dict[str, Any]:
        return {
            "property_id": connection["property_id"],
            "display_name": connection["display_name"],
            "email": connection["email"],
        }

    async def ga4_overview(self, user_id: int, project_id: str, **kwargs: Any) -> dict[str, Any]:
        """Organic totals against the previous equal-length period, plus a trend."""
        try:
            command = Ga4Overview.model_validate(kwargs)
        except ValidationError as exc:
            raise _error(ErrorCode.SEO_INVALID_INPUT) from exc
        connection, client = await self._ga4_bound(user_id, project_id)
        dates = self._ga4_dates(connection, command.start_date, command.end_date)
        start, end = dates["resolved"]["start_date"], dates["resolved"]["end_date"]
        previous = ga4.previous_period(start, end)
        requests = (
            ga4.overview_request(start, end),
            ga4.overview_request(previous["start_date"], previous["end_date"]),
            ga4.overview_request(start, end, command.trend),
        )
        prop = connection["property_id"]
        try:
            answers = await asyncio.gather(*(client.run_report(prop, body) for body in requests))
            current, before, trend = (
                ga4.normalize(answer, body) for answer, body in zip(answers, requests, strict=True)
            )
        except _GA4_ERRORS as exc:
            raise _ga4_failure(exc) from exc
        result = ga4.overview(current, before, trend)
        truncated = result.pop("trend_truncated")
        return {
            "source": self._ga4_source(connection),
            "request": {
                "requested_date_range": dates["requested"],
                "resolved_date_range": dates["resolved"],
                "previous_date_range": previous,
                "time_zone": connection["time_zone"],
                "currency_code": connection["currency_code"],
                "channel": "organic_search",
                "trend": command.trend,
            },
            **result,
            "warnings": dates["warnings"] + (["trend_truncated"] if truncated else []),
        }

    async def ga4_report(self, user_id: int, project_id: str, **kwargs: Any) -> dict[str, Any]:
        """One page of a report kind; ``next_offset`` continues it."""
        try:
            command = Ga4Report.model_validate(kwargs)
        except ValidationError as exc:
            raise _error(ErrorCode.SEO_INVALID_INPUT) from exc
        connection, client = await self._ga4_bound(user_id, project_id)
        dates = self._ga4_dates(connection, command.start_date, command.end_date)
        options = command.model_dump(
            exclude={"start_date", "end_date", "limit", "offset", "channel"}
        )
        options.pop("only_with_transactions")
        config = ga4.report_configuration(**options)
        body = ga4.report_request(
            **command.model_dump(exclude={"start_date", "end_date"}), **dates["resolved"]
        )
        try:
            report = ga4.normalize(await client.run_report(connection["property_id"], body), body)
        except _GA4_ERRORS as exc:
            raise _ga4_failure(exc) from exc
        rows = report["rows"]
        next_offset = command.offset + len(rows)
        has_more = next_offset < report["total_row_count"]
        config.pop("order")
        return {
            "source": self._ga4_source(connection),
            "request": {
                "requested_date_range": dates["requested"],
                "resolved_date_range": dates["resolved"],
                "time_zone": connection["time_zone"],
                "currency_code": connection["currency_code"],
                # Site search keeps every channel: its filter is the search event.
                "channel": "all" if command.kind == "site_search" else command.channel,
                **config,
                "limit": command.limit,
                "offset": command.offset,
            },
            "row_count": len(rows),
            "total_row_count": report["total_row_count"],
            "rows": rows,
            "page_info": {
                "offset": command.offset,
                "limit": command.limit,
                "has_more": has_more,
                "next_offset": next_offset if has_more else None,
            },
            "report_metadata": report["metadata"],
            "quota": report["quota"],
            "warnings": dates["warnings"],
        }


def _detail(exc: gsc.GoogleApiError) -> str:
    return f"{exc.status}: {_message(exc)}"
