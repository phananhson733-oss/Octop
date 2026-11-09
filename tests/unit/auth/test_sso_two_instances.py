"""Candidate protocol check with real signatures and an in-memory IdP transport.

No provider credentials, production databases, or external requests are used.
This proves callback isolation, not a deployed identity provider or gateway.
"""

import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from octop.config import OctopConfig
from octop.infra.auth.sso.service import SsoService
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.users.manager import UserManager
from octop.infra.utils.paths import PathLayout


@pytest.fixture
async def candidates(tmp_path):
    instances = []
    pools = []
    for name in ("internal", "customer"):
        paths = PathLayout(tmp_path / name)
        paths.ensure_root()
        db = SqlitePool(paths.db)
        pools.append(db)
        run_migrations(db)
        services = build_shared_services(
            db=db, paths=paths, config=OctopConfig(sso_auto_provision=False)
        )
        manager = UserManager(services)
        user = await manager.create(username="existing", password="TestPass12", role="user")
        provider = services.sso_repo.upsert_provider(
            enabled=True,
            display_name="Shared",
            issuer="https://identity.example",
            client_id=name,
            client_secret_enc=None,
            scopes="openid",
            dashboard_origin=None,
        )
        await manager.bind_sso_identity(
            user_id=user.id, provider_id=provider.id, subject="shared-person", claims={}
        )
        instances.append((name, SsoService(services, manager), user.id))
    yield instances
    for db in pools:
        db.close()


@pytest.fixture
def provider_transport(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="candidate", alg="RS256", use="sig")
    tokens = {}
    calls = []

    def handler(request):
        assert request.url.host == "identity.example"
        calls.append(request)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": "https://identity.example",
                    "authorization_endpoint": "https://identity.example/authorize",
                    "token_endpoint": "https://identity.example/token",
                    "jwks_uri": "https://identity.example/jwks",
                },
            )
        if request.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [jwk]})
        assert request.url.path == "/token"
        form = parse_qs(request.content.decode())
        assert form["code_verifier"]
        assert form["redirect_uri"][0].startswith("https://")
        return httpx.Response(200, json={"id_token": tokens[form["code"][0]]})

    monkeypatch.setattr(
        SsoService,
        "_http_client",
        lambda self: httpx.Client(transport=httpx.MockTransport(handler)),
    )

    def issue(code, audience, nonce, subject="shared-person", issuer="https://identity.example"):
        tokens[code] = jwt.encode(
            {
                "iss": issuer,
                "aud": audience,
                "sub": subject,
                "nonce": nonce,
                "iat": int(time.time()),
                "exp": int(time.time()) + 300,
            },
            key,
            algorithm="RS256",
            headers={"kid": "candidate"},
        )

    return issue, calls


async def test_shared_issuer_keeps_codes_and_accounts_local(candidates, provider_transport):
    issue, _calls = provider_transport
    for index, (name, service, uid) in enumerate(candidates):
        base = f"https://{name}.example"
        start = service.start_login(redirect_after="/chat", public_base=base)
        params = parse_qs(urlsplit(start["authorization_url"]).query)
        assert params["code_challenge_method"] == ["S256"]
        issue(name, name, params["nonce"][0])
        result = await service.handle_callback(
            code=name, state=params["state"][0], error=None, public_base=base
        )
        code = parse_qs(urlsplit(result.url).fragment)["code"][0]
        other = candidates[1 - index][1]
        with pytest.raises(ValueError):
            await other.exchange_login_code(code)
        assert (await service.exchange_login_code(code)).id == uid
        with pytest.raises(ValueError):
            await service.exchange_login_code(code)
        replay = await service.handle_callback(
            code=name, state=params["state"][0], error=None, public_base=base
        )
        assert "oidc_error=state" in replay.url
        assert service._user_manager.count() == 1


@pytest.mark.parametrize("case", ["wrong-audience", "wrong-nonce", "wrong-issuer", "unlinked"])
async def test_callback_rejects_untrusted_or_unlinked_identity(
    candidates, provider_transport, case
):
    issue, _calls = provider_transport
    name, service, _uid = candidates[1]
    base = "https://customer.example"
    start = service.start_login(redirect_after="/chat", public_base=base)
    params = parse_qs(urlsplit(start["authorization_url"]).query)
    issue(
        case,
        "internal" if case == "wrong-audience" else name,
        "wrong" if case == "wrong-nonce" else params["nonce"][0],
        subject="unknown" if case == "unlinked" else "shared-person",
        issuer="https://other.example" if case == "wrong-issuer" else "https://identity.example",
    )
    result = await service.handle_callback(
        code=case, state=params["state"][0], error=None, public_base=base
    )
    expected = "generic" if case == "unlinked" else "invalid_token"
    assert parse_qs(urlsplit(result.url).query)["oidc_error"] == [expected]
    assert not urlsplit(result.url).fragment
    assert service._user_manager.count() == 1
