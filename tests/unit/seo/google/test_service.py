"""Google sign-in, Search Console binding and reads against a fake Google."""

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from octop.infra.errors import OctopError
from octop.infra.seo.google import oauth
from octop.infra.seo.google.service import SeoGoogleService
from tests.unit.seo.google.fakegoogle import BASE, NOW, SITE, FakeGoogle


@pytest.fixture
def google_env(context, monkeypatch):
    seo, repos, user, other = context
    monkeypatch.setenv(oauth.CLIENT_ID_ENV, "client-1")
    monkeypatch.setenv(oauth.CLIENT_SECRET_ENV, "secret-1")
    fake = FakeGoogle()
    clock = {"now": NOW}
    service = SeoGoogleService(
        seo, transport=httpx.MockTransport(fake.handler), clock=lambda: clock["now"]
    )
    return service, seo, repos, user, other, fake, clock


async def _connect(service, fake, user, return_path="/workbench/seo-workbench.main?project=p"):
    started = await service.start(user, BASE, return_path)
    query = parse_qs(urlsplit(started["authorize_url"]).query)
    state = query["state"][0]
    fake.verifiers["code-1"] = query["code_challenge"][0]
    return state, query, await service.finish(state=state, code="code-1", error=None)


async def _project(seo, user):
    project = await seo.create_project(user, name="Example", domain="example.com", agent_id="seo")
    return project["project_id"]


async def test_connect_stores_an_encrypted_grant_and_burns_the_state(google_env):
    service, _seo, repos, user, _other, fake, _clock = google_env
    status = await service.status(user, BASE)
    assert status == {
        "configured": True,
        "redirect_uri": "https://octop.test/api/seo/google/callback",
        "accounts": [],
    }
    state, query, location = await _connect(service, fake, user)
    assert query["scope"][0].split() == list(oauth.SCOPES)
    assert (query["access_type"][0], query["code_challenge_method"][0]) == ("offline", "S256")
    assert location == "/workbench/seo-workbench.main?project=p&seo_google=connected"
    accounts = (await service.status(user, BASE))["accounts"]
    assert [(a["email"], a["search_console"], a["analytics"]) for a in accounts] == [
        ("owner@example.com", True, True)
    ]
    stored = repos.seo_google_repo.grants(user)[0]["credentials"]
    assert "at-1" not in stored and "rt-1" not in stored
    # A state works once.
    again = await service.finish(state=state, code="code-1", error=None)
    assert again == "/workbench?seo_google_error=invalid_state"


async def test_expired_unknown_and_refused_consents_go_back_with_a_reason(google_env):
    service, _seo, repos, user, other, fake, clock = google_env
    started = await service.start(user, BASE, "/workbench/seo-workbench.main")
    state = parse_qs(urlsplit(started["authorize_url"]).query)["state"][0]
    clock["now"] += oauth.STATE_TTL_S + 1
    assert (await service.finish(state=state, code="x", error=None)).endswith(
        "seo_google_error=invalid_state"
    )
    assert (await service.finish(state=None, code="x", error=None)).endswith("invalid_state")
    started = await service.start(user, BASE, "/workbench/seo-workbench.main")
    state = parse_qs(urlsplit(started["authorize_url"]).query)["state"][0]
    assert (
        await service.finish(state=state, code=None, error="access_denied")
        == "/workbench/seo-workbench.main?seo_google_error=access_denied"
    )
    # Only admins start a connection, and a full URL is not a place to come back to.
    with pytest.raises(OctopError) as denied:
        await service.start(other, BASE, None)
    assert denied.value.code == "FORBIDDEN"
    started = await service.start(user, BASE, "https://evil.example/steal")
    assert (
        repos.seo_google_repo.consume_state(
            parse_qs(urlsplit(started["authorize_url"]).query)["state"][0]
        )["return_path"]
        == "/workbench"
    )


async def test_a_grant_without_data_scopes_is_not_saved(google_env):
    service, _seo, repos, user, _other, fake, _clock = google_env
    fake.scope = "openid email"
    _state, _query, location = await _connect(service, fake, user)
    assert location.endswith("seo_google_error=scope_not_granted")
    assert repos.seo_google_repo.grants(user) == []


async def test_reconsent_keeps_the_refresh_token_google_left_out(google_env):
    service, _seo, repos, user, _other, fake, clock = google_env
    await _connect(service, fake, user)
    fake.send_refresh_token = False
    await _connect(service, fake, user)
    grants = repos.seo_google_repo.grants(user)
    assert len(grants) == 1
    stored = await service._credentials(grants[0])
    assert (stored["access_token"], stored["refresh_token"]) == ("at-2", "rt-1")
    # The kept refresh token still renews the access token once it expires.
    project_id = await _project(_seo, user)
    clock["now"] += 3600
    await service.gsc_sites(user, project_id)
    refresh = [form for kind, form in fake.calls if kind == "token"][-1]
    assert (refresh["grant_type"], refresh["refresh_token"]) == ("refresh_token", "rt-1")
    assert fake.calls[-1] == ("sites", {"auth": "Bearer at-3"})


async def test_a_refused_refresh_asks_for_a_reconnect(google_env):
    service, seo, _repos, user, _other, fake, clock = google_env
    await _connect(service, fake, user)
    project_id = await _project(seo, user)
    clock["now"] += 3600
    fake.refresh_ok = False
    listed = await service.gsc_sites(user, project_id)
    assert listed["accounts"][0]["requires_reconnect"] is True
    assert listed["accounts"][0]["sites"] == []


async def test_binding_needs_a_verified_property_on_the_grant(google_env):
    service, seo, repos, user, _other, fake, _clock = google_env
    await _connect(service, fake, user)
    project_id = await _project(seo, user)
    grant_id = repos.seo_google_repo.grants(user)[0]["grant_id"]
    sites = await service.gsc_sites(user, project_id)
    assert [s["site_url"] for s in sites["accounts"][0]["sites"]] == [
        SITE,
        "https://other.example/",
    ]
    for site_url, code in (
        ("https://missing.example/", "NOT_FOUND"),
        ("https://other.example/", "FORBIDDEN"),
    ):
        with pytest.raises(OctopError) as refused:
            await service.gsc_bind(user, project_id, grant_id=grant_id, site_url=site_url)
        assert refused.value.code == code
    with pytest.raises(OctopError) as unbound:
        await service.gsc_report(user, project_id)
    assert unbound.value.code == "SEO_GOOGLE_NOT_CONNECTED"
    connection = await service.gsc_bind(user, project_id, grant_id=grant_id, site_url=SITE)
    assert (connection["site_url"], connection["email"]) == (SITE, "owner@example.com")
    # Disconnecting the account drops the project's property with it.
    await service.delete_account(user, grant_id)
    assert await service.gsc_connection(user, project_id) is None


async def _bound(google_env):
    service, seo, repos, user, _other, fake, _clock = google_env
    await _connect(service, fake, user)
    project_id = await _project(seo, user)
    grant_id = repos.seo_google_repo.grants(user)[0]["grant_id"]
    await service.gsc_bind(user, project_id, grant_id=grant_id, site_url=SITE)
    fake.calls.clear()
    return service, user, project_id, fake


async def test_report_compares_periods_and_finds_striking_distance(google_env):
    service, user, project_id, fake = await _bound(google_env)
    fake.rows = {
        ("date",): [
            {"keys": ["2027-01-01"], "clicks": 10, "impressions": 100, "ctr": 0.1, "position": 4}
        ],
        ("query", "page"): [
            {
                "keys": ["kw", "https://e.com/a"],
                "clicks": 1,
                "impressions": 50,
                "ctr": 0.02,
                "position": 7,
            },
            {
                "keys": ["top", "https://e.com/b"],
                "clicks": 9,
                "impressions": 90,
                "ctr": 0.1,
                "position": 1.5,
            },
        ],
        ("country",): [
            {"keys": ["usa"], "clicks": 10, "impressions": 100, "ctr": 0.1, "position": 4}
        ],
    }
    report = await service.gsc_report(
        user, project_id, date_range="last_7_days", device="MOBILE", country="usa"
    )
    assert report["range"] == {
        "start_date": "2027-01-05",
        "end_date": "2027-01-12",
        "prev_start_date": "2026-12-28",
        "prev_end_date": "2027-01-04",
    }
    assert report["totals"]["clicks"] == 10 and report["prev_totals"]["clicks"] == 10
    assert [row["query"] for row in report["striking_distance"]] == ["kw"]
    assert report["countries"][0]["key"] == "usa"
    queries = {tuple(body["dimensions"]): body for kind, body in fake.calls if kind == "query"}
    device = {"dimension": "device", "operator": "equals", "expression": "MOBILE"}
    country = {"dimension": "country", "operator": "equals", "expression": "usa"}
    assert queries[("query", "page")]["dimensionFilterGroups"][0]["filters"] == [device, country]
    # The country list keeps every country selectable while one is chosen.
    assert queries[("country",)]["dimensionFilterGroups"][0]["filters"] == [device]


async def test_table_pages_with_one_extra_row(google_env):
    service, user, project_id, fake = await _bound(google_env)
    fake.rows = {
        ("query",): [
            {"keys": [f"q{i}"], "clicks": 1, "impressions": 1, "ctr": 1, "position": 1}
            for i in range(3)
        ]
    }
    table = await service.gsc_table(user, project_id, dimension="query", page=2, page_size=2)
    body = [b for kind, b in fake.calls if kind == "query"][0]
    assert (body["startRow"], body["rowLimit"]) == (2, 3)
    assert table["has_next_page"] and table["total_count"] is None and len(table["rows"]) == 2


async def test_performance_filters_positions_over_the_top_1000_rows(google_env):
    service, user, project_id, fake = await _bound(google_env)
    fake.rows = {
        ("query",): [
            {"keys": ["a"], "clicks": 5, "impressions": 80, "ctr": 0.0625, "position": 3.33},
            {"keys": ["b"], "clicks": 2, "impressions": 60, "ctr": 0.033333, "position": 8.04},
            {"keys": ["c"], "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 9},
        ]
    }
    data = await service.gsc_performance(
        user, project_id, min_position=5, max_position=20, min_impressions=50, row_limit=10
    )
    body = [b for kind, b in fake.calls if kind == "query"][0]
    assert body["rowLimit"] == 1000
    assert data["rows"] == [
        (1, {"keys": ["b"], "clicks": 2, "impressions": 60, "ctr": 0.0333, "position": 8.0})
    ]
    with pytest.raises(OctopError):
        await service.gsc_performance(user, project_id, start_date="2026-01-01")
    with pytest.raises(OctopError):
        await service.gsc_performance(user, project_id, dimensions=["searchAppearance", "query"])


async def test_inspection_reports_a_bad_url_next_to_the_good_ones(google_env):
    service, user, project_id, fake = await _bound(google_env)
    fake.inspect_status["https://elsewhere.example/"] = 403
    data = await service.gsc_inspect(
        user, project_id, urls=["https://elsewhere.example/", "https://example.com/"]
    )
    assert data["results"][0]["error"] == "403: not in property"
    assert data["results"][1]["result"]["indexStatusResult"]["verdict"] == "PASS"


async def test_google_errors_map_to_reconnect_or_api_error(google_env):
    service, seo, repos, user, _other, fake, _clock = google_env
    await _connect(service, fake, user)
    project_id = await _project(seo, user)
    grant_id = repos.seo_google_repo.grants(user)[0]["grant_id"]
    fake.sites_status, fake.sites_body = 403, '{"error": {"details": "SERVICE_DISABLED"}}'
    with pytest.raises(OctopError) as disabled:
        await service.gsc_bind(user, project_id, grant_id=grant_id, site_url=SITE)
    assert disabled.value.code == "SEO_GOOGLE_API"
    assert disabled.value.details == {"google_status": 403, "detail": "SERVICE_DISABLED"}
    assert disabled.value.status == 502
    listed = await service.gsc_sites(user, project_id)
    assert listed["accounts"][0]["api_disabled"] and not listed["accounts"][0]["requires_reconnect"]
    fake.sites_status, fake.sites_body = 401, "{}"
    with pytest.raises(OctopError) as dead:
        await service.gsc_bind(user, project_id, grant_id=grant_id, site_url=SITE)
    assert dead.value.code == "SEO_GOOGLE_RECONNECT"


async def test_without_a_client_nothing_starts(google_env, monkeypatch):
    service, _seo, _repos, user, _other, _fake, _clock = google_env
    monkeypatch.delenv(oauth.CLIENT_ID_ENV)
    assert (await service.status(user, BASE))["configured"] is False
    with pytest.raises(OctopError) as missing:
        await service.start(user, BASE, None)
    assert missing.value.code == "SEO_GOOGLE_NOT_CONFIGURED"


async def test_parallel_report_calls_refresh_an_expired_token_once(google_env):
    service, user, project_id, fake = await _bound(google_env)
    google_env[6]["now"] += 3600
    await service.gsc_report(user, project_id)
    refreshes = [f for kind, f in fake.calls if kind == "token"]
    assert [f["grant_type"] for f in refreshes] == ["refresh_token"]
    auths = {body["auth"] for kind, body in fake.calls if kind == "query"}
    assert auths == {"Bearer at-2"}
