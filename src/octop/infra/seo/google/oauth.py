"""Google sign-in for the SEO workbench: consent URL, code exchange and token refresh.

Ported from OpenSEO d7ea785 ``server/features/google/googleOAuth.ts``. One grant covers
Search Console and Analytics, both read-only, so an admin consents once; OpenSEO asks
for each separately. The OAuth client comes from ``OCTOP_SEO_GOOGLE_CLIENT_ID`` and
``OCTOP_SEO_GOOGLE_CLIENT_SECRET``, read per request.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import jwt

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GSC_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
GA4_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
SCOPES = ("openid", "email", GSC_SCOPE, GA4_SCOPE)
CALLBACK_PATH = "/api/seo/google/callback"
CLIENT_ID_ENV = "OCTOP_SEO_GOOGLE_CLIENT_ID"
CLIENT_SECRET_ENV = "OCTOP_SEO_GOOGLE_CLIENT_SECRET"
STATE_TTL_S = 600
# Refresh an access token this many seconds before Google says it expires.
REFRESH_SKEW_S = 60
_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})


class GoogleRejected(Exception):
    """Google's token endpoint refused the code or the refresh token."""


def client_config() -> tuple[str, str] | None:
    client_id = os.environ.get(CLIENT_ID_ENV, "").strip()
    client_secret = os.environ.get(CLIENT_SECRET_ENV, "").strip()
    return (client_id, client_secret) if client_id and client_secret else None


def safe_return_path(raw: str | None, default: str) -> str:
    """A same-origin path to come back to; ``//host`` and full URLs fall back to ``default``."""
    path = (raw or "").strip()
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return default
    parts = urlsplit(path)
    return default if parts.scheme or parts.netloc else path


def authorize_url(*, client_id: str, redirect_uri: str, state: str, code_challenge: str) -> str:
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        # Offline access plus a forced consent screen, so Google returns a refresh token.
        "access_type": "offline",
        "prompt": "select_account consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urlencode(query)}"


async def fetch_tokens(
    params: dict[str, str], *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30, transport=transport) as client:
        response = await client.post(TOKEN_URL, data=params)
    if response.status_code != 200:
        raise GoogleRejected(f"token endpoint answered {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise GoogleRejected("token endpoint answered with no JSON") from exc
    if not isinstance(body, dict) or not isinstance(body.get("access_token"), str):
        raise GoogleRejected("token endpoint answered without an access token")
    return body


def granted_scopes(tokens: dict[str, Any]) -> list[str]:
    """Scopes Google says it granted; an omitted ``scope`` means all that were asked for."""
    raw = tokens.get("scope")
    return str(raw).split() if isinstance(raw, str) and raw.strip() else list(SCOPES)


def google_account(id_token: str, client_id: str, now: float) -> tuple[str, str | None]:
    """The Google account id (``sub``) and email from the ID token.

    The token comes straight from Google's token endpoint over TLS, which OpenID Connect
    Core 3.1.3.7 lets stand in for checking its signature; audience, issuer and expiry
    are still checked.
    """
    try:
        claims = jwt.decode(id_token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise GoogleRejected("ID token is not a JWT") from exc
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    exp = claims.get("exp")
    sub = claims.get("sub")
    if (
        client_id not in audiences
        or claims.get("iss") not in _ISSUERS
        or not isinstance(exp, int | float)
        or exp < now
        or not isinstance(sub, str)
        or not sub
    ):
        raise GoogleRejected("ID token is not for this client or has expired")
    email = claims.get("email")
    return sub, email if isinstance(email, str) and email else None


def credentials(
    tokens: dict[str, Any], now: float, previous_refresh_token: str | None = None
) -> dict[str, Any]:
    """What a grant stores. Google may leave the refresh token out of a later consent."""
    expires_in = tokens.get("expires_in")
    lifetime = expires_in if isinstance(expires_in, int | float) and expires_in > 0 else 3600
    refresh = tokens.get("refresh_token")
    return {
        "access_token": tokens["access_token"],
        "refresh_token": refresh
        if isinstance(refresh, str) and refresh
        else previous_refresh_token,
        "expires_at": int(now + lifetime),
    }


def needs_refresh(stored: dict[str, Any], now: float) -> bool:
    expires_at = stored.get("expires_at")
    return not isinstance(expires_at, int | float) or expires_at - now <= REFRESH_SKEW_S
