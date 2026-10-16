from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import RepoBundle
from octop.infra.seo.service import SeoService


@pytest.fixture
def context(tmp_path: Path):
    pool = SqlitePool(tmp_path / "seo.db")
    run_migrations(pool)
    repos = RepoBundle.from_pool(pool)
    user = repos.user_repo.create(username="alice", role="admin")
    other = repos.user_repo.create(username="bob", role="user")
    repos.agent_repo.create(agent_id="seo", user_id=user, name="SEO")
    yield SeoService(repos), repos, user, other
    pool.close()
