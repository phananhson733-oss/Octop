"""Access-token JWT helpers shared by HTTP auth and infra (Bridge)."""

from __future__ import annotations

import secrets
import time
from typing import Any

import jwt


class InvalidToken(Exception): ...


class TokenExpired(InvalidToken): ...


def sign_token(
    secret: bytes,
    *,
    sub: int,
    uname: str,
    role: str,
    ttl_seconds: int = 86400,
    session_id: str | None = None,
) -> str:
    now = int(time.time())
    payload = {
        "sid": session_id or secrets.token_urlsafe(32),
        "sub": str(sub),
        "uname": uname,
        "role": role,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_token(secret: bytes, token: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        if "sub" in payload:
            payload["sub"] = int(payload["sub"])
        return payload
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpired() from exc
    except jwt.InvalidTokenError as exc:
        raise InvalidToken(str(exc)) from exc


def decode_revocation_token(secret: bytes, token: str) -> dict[str, Any]:
    """Verify a signed credential solely to revoke its family, never to log in.

    Expired tokens may still identify a live renewal family. All other signature
    and claim checks remain enabled; callers must not use this for authorization.
    """
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"verify_exp": False, "require": ["sub", "iat", "exp"]},
        )
        payload["sub"] = int(payload["sub"])
        return payload
    except (jwt.InvalidTokenError, ValueError, TypeError) as exc:
        raise InvalidToken("invalid revocation credential") from exc
