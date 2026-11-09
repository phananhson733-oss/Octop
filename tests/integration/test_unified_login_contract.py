"""Native password-login prerequisites for the unified entry (no production data)."""

from tests.support.auth import TEST_PASSWORD, bearer, login


async def test_password_user_disable_invalidates_existing_access(env):
    client, _server, admin = env
    created = await client.post(
        "/api/users",
        headers=admin,
        json={"username": "customer_login", "password": TEST_PASSWORD, "role": "user"},
    )
    assert created.status_code == 201, created.text
    auth = bearer(await login(client, username="customer_login"))
    assert (await client.get("/api/auth/me", headers=auth)).status_code == 200
    disabled = await client.patch(
        f"/api/users/{created.json()['id']}", headers=admin, json={"disabled": True}
    )
    assert disabled.status_code == 200, disabled.text
    assert (await client.get("/api/auth/me", headers=auth)).status_code in (401, 403)


async def test_logout_revokes_the_presented_session(env):
    client, _server, _admin = env
    token = await login(client)
    auth = bearer(token)
    assert (await client.get("/api/auth/me", headers=auth)).status_code == 200
    assert (await client.post("/api/auth/logout", headers=auth)).status_code == 204
    assert (await client.get("/api/auth/me", headers=auth)).status_code == 401


async def test_logout_revokes_renewals_but_not_another_login(env):
    from octop.api.deps import ACCESS_TOKEN_RESPONSE_HEADER
    from octop.infra.auth.sessions import assert_session_active
    from octop.infra.auth.tokens import InvalidToken, decode_token
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.services import build_shared_services

    client, server, _admin = env
    # Short-lived originals immediately qualify for renewal after restoring TTL.
    config = server.services.config
    from unittest.mock import patch

    from octop.infra.auth.tokens import sign_token

    def short_login(secret, **kwargs):
        return sign_token(secret, **{**kwargs, "ttl_seconds": 10})

    with patch("octop.api.routers.auth.sign_token", side_effect=short_login):
        first = await login(client)
        second = await login(client)
    assert first != second
    response = await client.get("/api/auth/me", headers=bearer(first))
    renewed = response.headers[ACCESS_TOKEN_RESPONSE_HEADER]
    secret = server.services.secret_repo.get("jwt")
    assert decode_token(secret, first)["sid"] == decode_token(secret, renewed)["sid"]
    logout = await client.post("/api/auth/logout", headers=bearer(first))
    assert logout.status_code == 204
    assert ACCESS_TOKEN_RESPONSE_HEADER not in logout.headers
    for token in (first, renewed):
        assert (await client.get("/api/auth/me", headers=bearer(token))).status_code == 401
    assert (await client.get("/api/auth/me", headers=bearer(second))).status_code == 200
    # A fresh DB connection and fresh services see the durable revocation.
    db = SqlitePool(server.services.paths.db)
    try:
        reopened = build_shared_services(db=db, paths=server.services.paths, config=config)
        import pytest

        with pytest.raises(InvalidToken):
            assert_session_active(reopened, decode_token(secret, renewed), renewed)
        assert_session_active(reopened, decode_token(secret, second), second)
    finally:
        db.close()


async def test_revocation_receipt_accepts_expired_signature_only_for_revoking(env):
    import time

    import jwt

    from octop.infra.auth.tokens import sign_token

    client, server, _admin = env
    secret = server.services.secret_repo.get("jwt")
    now = int(time.time())
    expired = jwt.encode(
        {
            "sub": "1",
            "uname": "admin",
            "role": "admin",
            "sid": "receipt-family",
            "iat": now - 60,
            "exp": now - 1,
        },
        secret,
        algorithm="HS256",
    )
    renewal = sign_token(secret, sub=1, uname="admin", role="admin", session_id="receipt-family")
    other = await login(client)
    assert (await client.get("/api/auth/me", headers=bearer(expired))).status_code == 401
    assert (await client.get("/api/auth/me", headers=bearer(renewal))).status_code == 200
    for _ in range(2):
        receipt = await client.post("/api/auth/session/revoke", headers=bearer(expired))
        assert receipt.status_code == 204, receipt.text
        assert receipt.headers["x-octop-session-revoked"] == "1"
    assert (await client.get("/api/auth/me", headers=bearer(renewal))).status_code == 401
    assert (await client.get("/api/auth/me", headers=bearer(other))).status_code == 200
    assert (
        await client.post("/api/auth/session/revoke", headers=bearer("invalid.signature"))
    ).status_code == 401
    assert (
        await client.post("/api/auth/session/revoke", params={"access_token": other})
    ).status_code == 401
    assert (await client.get("/api/auth/me", headers=bearer(other))).status_code == 200
