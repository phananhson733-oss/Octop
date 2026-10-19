"""A fake Google: token endpoint, Search Console sites, Search Analytics, URL Inspection."""

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt

from octop.infra.seo.google import ga4, oauth

NOW = 1_800_000_000.0
BASE = "https://octop.test"
SITE = "sc-domain:example.com"
PROPERTY = "properties/123"


class FakeGoogle:
    """Token endpoint, Search Console (sites, Search Analytics, URL Inspection) and
    Analytics (account summaries, property, runReport)."""

    def __init__(self, base: str = BASE) -> None:
        self.base = base
        self.calls: list[tuple[str, dict]] = []
        self.verifiers: dict[str, str] = {}
        self.tokens = 0
        self.refresh_ok = True
        self.scope = " ".join(oauth.SCOPES)
        self.send_refresh_token = True
        self.sites = [
            {"siteUrl": SITE, "permissionLevel": "siteOwner"},
            {"siteUrl": "https://other.example/", "permissionLevel": "siteUnverifiedUser"},
        ]
        self.sites_status = 200
        self.sites_body = ""
        self.rows: dict[tuple[str, ...], list[dict]] = {}
        self.inspect_status: dict[str, int] = {}
        # Two pages of account summaries, so paging is exercised.
        self.summaries = [
            {
                "accountSummaries": [
                    {
                        "account": "accounts/1",
                        "displayName": "Example Co",
                        "propertySummaries": [{"property": PROPERTY, "displayName": "example.com"}],
                    }
                ],
                "nextPageToken": "page 2",
            },
            {
                "accountSummaries": [
                    {
                        "account": "accounts/2",
                        "displayName": "Other",
                        "propertySummaries": [
                            {"property": "properties/999", "displayName": "other"}
                        ],
                    }
                ]
            },
        ]
        self.ga4_status = 200
        self.ga4_body = ""
        # runReport answers by dimension names; the default is a headerless empty report.
        self.reports: dict[tuple[str, ...], dict] = {}

    def id_token(self, sub: str = "g-1") -> str:
        claims = {
            "iss": "https://accounts.google.com",
            "aud": "client-1",
            "sub": sub,
            "email": "owner@example.com",
            "exp": int(NOW) + 3600,
        }
        return jwt.encode(claims, "unused-signing-key-of-32-bytes!!", algorithm="HS256")

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == oauth.TOKEN_URL:
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.calls.append(("token", form))
            assert form["client_id"] == "client-1" and form["client_secret"] == "secret-1"
            if form["grant_type"] == "authorization_code":
                challenge = self.verifiers[form["code"]]
                digest = hashlib.sha256(form["code_verifier"].encode()).digest()
                assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == challenge
                assert form["redirect_uri"] == self.base + oauth.CALLBACK_PATH
                self.tokens += 1
                body = {
                    "access_token": f"at-{self.tokens}",
                    "expires_in": 3600,
                    "scope": self.scope,
                    "id_token": self.id_token(),
                }
                if self.send_refresh_token:
                    body["refresh_token"] = f"rt-{self.tokens}"
                return httpx.Response(200, json=body)
            if not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.tokens += 1
            return httpx.Response(
                200, json={"access_token": f"at-{self.tokens}", "expires_in": 3600}
            )
        auth = request.headers.get("authorization", "")
        if url.endswith("/webmasters/v3/sites"):
            self.calls.append(("sites", {"auth": auth}))
            if self.sites_status != 200:
                return httpx.Response(self.sites_status, text=self.sites_body)
            return httpx.Response(200, json={"siteEntry": self.sites})
        if "/searchAnalytics/query" in url:
            body = json.loads(request.content)
            self.calls.append(("query", body | {"auth": auth}))
            return httpx.Response(200, json={"rows": self.rows.get(tuple(body["dimensions"]), [])})
        if url.endswith("index:inspect"):
            body = json.loads(request.content)
            self.calls.append(("inspect", body))
            status = self.inspect_status.get(body["inspectionUrl"], 200)
            if status != 200:
                return httpx.Response(status, json={"error": {"message": "not in property"}})
            return httpx.Response(
                200,
                json={
                    "inspectionResult": {
                        "indexStatusResult": {
                            "verdict": "PASS",
                            "referringUrls": [f"https://e.com/{i}" for i in range(8)],
                        }
                    }
                },
            )
        if url.startswith((ga4.ADMIN_API, ga4.DATA_API)) and self.ga4_status != 200:
            return httpx.Response(self.ga4_status, text=self.ga4_body)
        if url.startswith(f"{ga4.ADMIN_API}/accountSummaries"):
            page = parse_qs(urlsplit(url).query).get("pageToken", [""])[0]
            self.calls.append(("summaries", {"page": page, "auth": auth}))
            return httpx.Response(200, json=self.summaries[1 if page == "page 2" else 0])
        if url == f"{ga4.ADMIN_API}/{PROPERTY}":
            self.calls.append(("property", {}))
            return httpx.Response(
                200,
                json={
                    "name": PROPERTY,
                    "displayName": "example.com",
                    "timeZone": "America/New_York",
                    "currencyCode": "USD",
                },
            )
        if url == f"{ga4.DATA_API}/{PROPERTY}:runReport":
            body = json.loads(request.content)
            self.calls.append(("ga4", body | {"auth": auth}))
            names = tuple(d["name"] for d in body["dimensions"])
            return httpx.Response(200, json=self.reports.get(names, {}))
        raise AssertionError(f"unexpected request {url}")
