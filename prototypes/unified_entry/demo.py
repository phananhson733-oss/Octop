"""Local browser demo with synthetic data and fake model, never production credentials."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
from contextlib import AsyncExitStack, nullcontext
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn
from cryptography.fernet import Fernet


async def run(port: int, sab_ui: Path, check_isolation: bool = False) -> None:
    if "octop_browser.settings" in sys.modules:
        raise RuntimeError("Run the demo in a fresh process before browser settings are imported")
    with tempfile.TemporaryDirectory(prefix="octop-entry-demo-") as temporary:
        root = Path(temporary)
        user_home = root / "home"
        user_home.mkdir()
        # Child-process isolation: do not inherit a production database override.
        for name in list(os.environ):
            if name.startswith(("OCTOP_", "BROWSER_USE_", "BROWSER_RECORD_")):
                del os.environ[name]
        os.environ["BROWSER_USE_PROFILES_DIR"] = str(root / "browser-profiles")
        os.environ["BROWSER_USE_SCREENSHOTS_DIR"] = str(root / "browser-screenshots")
        os.environ["BROWSER_RECORD_REPLAY_DIR"] = str(root / "record-replay")
        os.environ["BROWSER_USE_CDP_HOST"] = "127.0.0.1"
        with patch.object(Path, "home", return_value=user_home):
            from prototypes.unified_entry.app import build_candidate
            from tests.support.app import octop_client
            from tests.support.auth import (
                TEST_PASSWORD,
                bearer,
                bootstrap_admin,
                login,
                seed_openai_provider,
            )
            from tests.support.fakes import FakeHarnessAgent

            from octop.config import OctopConfig
            from octop.infra.agents.manager import AgentCreateSpec
            from octop.infra.db.migrate import run_migrations
            from octop.infra.db.pool import SqlitePool
            from octop.infra.db.services import build_shared_services
            from octop.infra.unified_entry.store import Binding, EntryStore
            from octop.infra.utils.paths import PathLayout

            if check_isolation:
                from octop_browser.record.settings import settings as record_settings
                from octop_browser.settings import settings as browser_settings

                assert browser_settings.profiles_dir.is_relative_to(root)
                assert browser_settings.screenshots_dir.is_relative_to(root)
                assert record_settings.base_dir.is_relative_to(root)
                assert browser_settings.cdp_host == "127.0.0.1"
                assert browser_settings.cdp_ws_url is None
                assert "OCTOP_DATABASE_URL" not in os.environ
                print(json.dumps({"isolated": True}))
                return

            async with AsyncExitStack() as stack:
                clients, bindings, servers, tasks = {}, {}, [], []
                try:
                    for name in ("internal", "customer"):
                        home = root / name
                        fake = FakeHarnessAgent(
                            chunks=[
                                {
                                    "type": "token",
                                    "node": "agent",
                                    "content": "候选环境回复：这是合成数据验证，没有调用真实模型。",
                                }
                            ]
                        )
                        control, native = await stack.enter_async_context(
                            octop_client(home, fake_agent=fake)
                        )
                        admin_password = secrets.token_urlsafe(24) + "Aa1"
                        await bootstrap_admin(control, home, password=admin_password)
                        admin = bearer(await login(control, password=admin_password))
                        await seed_openai_provider(control, admin)
                        created = await control.post(
                            "/api/users",
                            headers=admin,
                            json={"username": "demo", "password": TEST_PASSWORD, "role": "user"},
                        )
                        created.raise_for_status()
                        uid = created.json()["id"]
                        row = await native.app_runtime.agent_registry.create(
                            AgentCreateSpec(
                                agent_id="DEMO01",
                                user_id=uid,
                                name="选剧专家（候选）"
                                if name == "customer"
                                else "内部专家（候选）",
                                config={"providers": ["openai"], "default_model": "openai:gpt-4o"},
                            )
                        )
                        aid = row.agent_id
                        native.services.agent_repo.update_config(
                            aid, published_expert_id="demo-selection"
                        )
                        plugin = root / (name + "-plugin")
                        (plugin / "ui" / "data").mkdir(parents=True)
                        (plugin / "plugin.yaml").write_text(
                            "id: sab-tools\nversion: 0.1.0\nname: 选剧推荐\nkind: tool\nentry: main.py\nui:\n  entry: ui/index.js\n  manifest: ui/manifest.json\n"
                        )
                        (plugin / "main.py").write_text("def setup(ctx):\n    pass\n")
                        shutil.copyfile(sab_ui, plugin / "ui" / "index.js")
                        (plugin / "ui" / "manifest.json").write_text('{"renderers": []}')
                        (plugin / "ui" / "data" / "sab.json").write_text(
                            json.dumps(
                                {
                                    "clip": True,
                                    "columns": [
                                        "sab_id",
                                        "title",
                                        "title_cn",
                                        "language",
                                        "language_zh",
                                        "theater",
                                        "grade",
                                        "kind",
                                        "tags",
                                        "episodes",
                                        "listed_at",
                                    ],
                                    "rows": [
                                        [
                                            "demo-1",
                                            "Candidate Drama",
                                            "候选演示剧",
                                            "en",
                                            "英语",
                                            "Demo Theater",
                                            "S",
                                            "剧情",
                                            "演示",
                                            12,
                                            "2026-11-06",
                                        ]
                                    ],
                                    "eval_day": "2026-11-06",
                                },
                                ensure_ascii=False,
                            )
                        )
                        native.plugin_manager.install_path(plugin)
                        role = await control.post(
                            "/api/users/roles",
                            headers=admin,
                            json={
                                "user_role_name": name + " demo",
                                "permissions": ["experts"],
                                "policies": [
                                    {"name": "visible_nav", "value": "chat,experts,workbench"},
                                    {"name": "hidden_expert_tabs", "value": "teams,library,market"},
                                    {"name": "allowed_plugin_ui", "value": '["sab-tools"]'},
                                    {
                                        "name": "allowed_expert_templates",
                                        "value": '["demo-selection"]',
                                    },
                                ],
                            },
                        )
                        role.raise_for_status()
                        assigned = await control.patch(
                            f"/api/users/{uid}",
                            headers=admin,
                            json={"role": role.json()["user_role_id"]},
                        )
                        assigned.raise_for_status()
                        bindings[name + "/demo"] = Binding("demo-" + name, name, "demo", uid)
                        sock = socket.socket()
                        sock.bind(("127.0.0.1", 0))
                        sock.listen(64)
                        address = f"http://127.0.0.1:{sock.getsockname()[1]}"
                        server = uvicorn.Server(
                            uvicorn.Config(
                                control._octop_app,
                                lifespan="off",
                                log_level="error",
                                access_log=False,
                            )
                        )
                        server.capture_signals = nullcontext
                        servers.append(server)
                        tasks.append(asyncio.create_task(server.serve(sockets=[sock])))
                        while not server.started:
                            if tasks[-1].done():
                                await tasks[-1]
                            await asyncio.sleep(0.01)
                        clients[name] = await stack.enter_async_context(
                            httpx.AsyncClient(base_url=address, trust_env=False)
                        )
                    paths = PathLayout(root / "entry")
                    paths.ensure_root()
                    db = SqlitePool(paths.db)
                    stack.callback(db.close)
                    run_migrations(db)
                    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
                    store = EntryStore(services, Fernet.generate_key())
                    dashboard = Path(__file__).resolve().parents[2] / "src/octop/dashboard"
                    app = build_candidate(clients, bindings, store, dashboard=dashboard)
                    print(
                        f"SYNTHETIC CANDIDATE http://127.0.0.1:{port} | internal/demo or customer/demo | password {TEST_PASSWORD}",
                        flush=True,
                    )
                    await uvicorn.Server(
                        uvicorn.Config(
                            app, host="127.0.0.1", port=port, log_level="error", access_log=False
                        )
                    ).serve()
                finally:
                    for server in servers:
                        server.should_exit = True
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--sab-ui", type=Path, required=True)
    parser.add_argument("--check-isolation", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.port, args.sab_ui, args.check_isolation))
