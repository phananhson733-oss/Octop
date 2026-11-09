"""Legacy-token renewal shares a durable revocation family."""

import secrets
import time
from types import SimpleNamespace

import jwt
import pytest

from octop.api.deps import maybe_sliding_renew_token
from octop.config import OctopConfig
from octop.infra.auth.sessions import assert_session_active, revoke_session
from octop.infra.auth.tokens import InvalidToken, decode_token
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout


def test_legacy_renewal_is_revoked_with_original(tmp_path):
    paths = PathLayout(tmp_path)
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    secret = services.secret_repo.get_or_create("jwt", lambda: secrets.token_bytes(32))
    now = int(time.time())
    original = jwt.encode(
        {"sub": "1", "uname": "alice", "role": "user", "iat": now, "exp": now + 10},
        secret,
        algorithm="HS256",
    )
    try:
        user = SimpleNamespace(id=1, username="alice", role="user")
        renewed = maybe_sliding_renew_token(SimpleNamespace(services=services), original, user)
        assert renewed is not None
        revoke_session(services, original)
        for token in (original, renewed):
            with pytest.raises(InvalidToken):
                assert_session_active(services, decode_token(secret, token), token)
    finally:
        db.close()
