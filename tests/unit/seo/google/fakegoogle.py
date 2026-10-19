"""A fake Google: token endpoint, Search Console sites, Search Analytics, URL Inspection."""

import base64
import hashlib
import json
from urllib.parse import parse_qs

import httpx
import jwt

from octop.infra.seo.google import oauth

NOW = 1_800_000_000.0
BASE = "https://octop.test"
SITE = "sc-domain:example.com"


class FakeGoogle:
    """Token endpoint, Search Console sites, Search Analytics and URL Inspection."""

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
        raise AssertionError(f"unexpected request {url}")
