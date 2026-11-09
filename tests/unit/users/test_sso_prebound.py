"""Two isolated instance databases retain local users under one SSO subject."""

import pytest

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.manager import UserManager
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def instances(tmp_path):
    pools = []
    managers = []
    for name in ("internal", "customer"):
        paths = PathLayout(tmp_path / name)
        paths.ensure_root()
        db = SqlitePool(paths.db)
        pools.append(db)
        run_migrations(db)
        services = build_shared_services(
            db=db, paths=paths, config=OctopConfig(sso_auto_provision=False)
        )
        provider = services.sso_repo.upsert_provider(
            enabled=True,
            display_name="Shared identity",
            issuer="https://identity.example",
            client_id=name,
            client_secret_enc=None,
            scopes="openid",
            dashboard_origin=None,
        )
        managers.append((UserManager(services), provider.id))
    yield managers
    for db in pools:
        db.close()


async def test_two_instances_require_explicit_binding_and_keep_local_identity(instances):
    for manager, provider_id in instances:
        # Same claimed name/email must not implicitly link to the existing account.
        local = await manager.create(username="existing", password="TestPass12", role="user")
        claims = {"preferred_username": "existing", "email": "same@example.com"}
        before = manager.count()
        with pytest.raises(OctopError) as denied:
            await manager.resolve_or_create_sso_user(
                provider_id=provider_id, subject="global-subject", claims=claims
            )
        assert denied.value.code == ErrorCode.FORBIDDEN
        assert manager.count() == before
        await manager.bind_sso_identity(
            user_id=local.id, provider_id=provider_id, subject="global-subject", claims=claims
        )
        for _ in range(2):
            resolved = await manager.resolve_or_create_sso_user(
                provider_id=provider_id, subject="global-subject", claims=claims
            )
            assert resolved.id == local.id
            assert resolved.username == "existing"
            assert resolved.permissions == local.permissions
        assert manager.count() == before
        await manager.disable("existing")
        with pytest.raises(OctopError) as disabled:
            await manager.resolve_or_create_sso_user(
                provider_id=provider_id, subject="global-subject", claims=claims
            )
        assert disabled.value.code == ErrorCode.USER_DISABLED
