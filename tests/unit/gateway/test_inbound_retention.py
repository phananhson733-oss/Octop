"""Old audio/video chat attachments are deleted from ``inbound/``; the rest stays."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from octop.config import load_config
from octop.infra.gateway.media.inbound_retention import (
    sweep_inbound_media,
    sweep_inbound_media_forever,
)

NOW = 1_800_000_000
DAY = 24 * 60 * 60


class FakeWorkspace:
    """``inbound/`` holding *names*; a name with ``/`` lives in a subfolder."""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.deleted: list[str] = []

    async def als(self, path: str = ".") -> Any:
        prefix = path.removeprefix("inbound").strip("/")
        entries: dict[str, bool] = {}
        for name in self.names:
            if prefix and not name.startswith(prefix + "/"):
                continue
            rest = name[len(prefix) + 1 :] if prefix else name
            head, _, tail = rest.partition("/")
            entries[f"{path}/{head}"] = bool(tail)
        if not prefix:
            entries[f"{path}/empty"] = True
        return SimpleNamespace(
            error=None, entries=[{"path": p, "is_dir": d} for p, d in entries.items()]
        )

    async def adelete(self, path: str) -> None:
        self.deleted.append(path)


async def test_deletes_only_old_audio_video() -> None:
    old = NOW - 8 * DAY
    ws = FakeWorkspace(
        [
            f"{old}_ep1.mp4",
            f"{old}_voice.m4a",
            f"{old}_cut.MKV",
            f"{old}_poster.png",
            f"{old}_notes.pdf",
            f"{NOW - 6 * DAY}_ep2.mp4",
            "ep3.mp4",
        ]
    )
    removed = await sweep_inbound_media(ws, retention_days=7, now=NOW)  # type: ignore[arg-type]
    assert removed == [
        f"inbound/{old}_ep1.mp4",
        f"inbound/{old}_voice.m4a",
        f"inbound/{old}_cut.MKV",
    ]
    assert ws.deleted == removed


async def test_reaches_im_channel_subfolders() -> None:
    old = NOW - 8 * DAY
    ws = FakeWorkspace([f"feishu/oc_1/{old}_voice.mp3", f"feishu/oc_1/{old}_pic.jpg"])
    removed = await sweep_inbound_media(ws, retention_days=7, now=NOW)  # type: ignore[arg-type]
    assert removed == [f"inbound/feishu/oc_1/{old}_voice.mp3"]


async def test_missing_inbound_dir_is_a_no_op() -> None:
    ws = SimpleNamespace(als=lambda path=".": asyncio.sleep(0, None))
    assert await sweep_inbound_media(ws, retention_days=7, now=NOW) == []  # type: ignore[arg-type]


async def test_forever_keeps_going_when_one_agent_fails() -> None:
    good = FakeWorkspace([f"{int(time.time()) - 30 * DAY}_ep.mp4"])

    class Broken:
        async def als(self, path: str = ".") -> Any:
            raise RuntimeError("backend down")

    task = asyncio.create_task(
        sweep_inbound_media_forever(
            lambda: [("bad", Broken()), ("good", good)],  # type: ignore[list-item]
            retention_days=7,
            interval_seconds=3600,
        )
    )
    for _ in range(50):
        if good.deleted:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(good.deleted) == 1


async def test_forever_survives_a_failed_agent_listing() -> None:
    good = FakeWorkspace([f"{int(time.time()) - 30 * DAY}_ep.mp4"])
    calls = 0

    def listing() -> list[tuple[str, Any]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database is locked")
        return [("good", good)]

    task = asyncio.create_task(
        sweep_inbound_media_forever(listing, retention_days=7, interval_seconds=0.01)
    )
    for _ in range(100):
        if good.deleted:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls >= 2
    assert good.deleted


def test_retention_days_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCTOP_INBOUND_MEDIA_RETENTION_DAYS", raising=False)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{}")
    assert load_config(cfg_path).inbound_media_retention_days == 7
    cfg_path.write_text(json.dumps({"inbound_media_retention_days": 0}))
    assert load_config(cfg_path).inbound_media_retention_days == 0
    monkeypatch.setenv("OCTOP_INBOUND_MEDIA_RETENTION_DAYS", "3")
    assert load_config(cfg_path).inbound_media_retention_days == 3
