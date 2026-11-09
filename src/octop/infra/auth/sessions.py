"""Persistent session revocation shared by all HTTP workers on this database.

Revocations intentionally have no automatic TTL: sliding sessions can outlive
any individual token. Never remove a record based on that token's expiration.
"""

from __future__ import annotations

import hashlib
from typing import Any

from octop.infra.auth.tokens import InvalidToken, decode_token
from octop.infra.db.services import SharedServices


def session_id(payload: dict[str, Any], token: str) -> str:
    """Use the signed session id; old tokens acquire a stable family on renewal."""
    sid = payload.get("sid")
    if sid is None:
        return "legacy-" + hashlib.sha256(token.encode()).hexdigest()
    if not isinstance(sid, str) or not sid:
        raise InvalidToken("invalid session id")
    return sid


def _key(sid: str) -> str:
    return "auth.revoked_session." + hashlib.sha256(sid.encode()).hexdigest()


def assert_session_active(services: SharedServices, payload: dict[str, Any], token: str) -> None:
    if services.settings_repo.get(_key(session_id(payload, token))) is not None:
        raise InvalidToken("session revoked")


def revoke_session(services: SharedServices, token: str) -> None:
    secret = services.secret_repo.get("jwt")
    if secret is None:
        raise InvalidToken("jwt secret missing")
    payload = decode_token(secret, token)
    services.settings_repo.set(_key(session_id(payload, token)), "1")


REVOCATION_RECEIPT_HEADER = "X-Octop-Session-Revoked"


def revoke_signed_session(services: SharedServices, token: str) -> None:
    """Idempotent, durable revoke-only operation, including already expired tokens."""
    from octop.infra.auth.tokens import decode_revocation_token

    secret = services.secret_repo.get("jwt")
    if secret is None:
        raise InvalidToken("jwt secret missing")
    payload = decode_revocation_token(secret, token)
    services.settings_repo.set_if_absent(_key(session_id(payload, token)), "1")
    services.audit_repo.write(
        actor=str(payload.get("uname") or payload["sub"]),
        action="auth.session_revoke",
        target=str(payload["sub"]),
    )
