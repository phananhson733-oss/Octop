"""Tests for BinaryReadGuardMiddleware."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from octop.infra.agents.middleware.binary_read_guard import (
    BinaryReadGuardMiddleware,
    read_file_block_reason,
)


def test_blocks_inbound_pdf() -> None:
    reason = read_file_block_reason("inbound/01KWB9MG2Z570P7QB367KDJ75R.pdf")
    assert reason is not None
    assert "pdf" in reason.lower()
    assert "execute_shell_command" in reason


def test_allows_inbound_txt() -> None:
    assert read_file_block_reason("inbound/01JTEST.txt") is None


def test_blocks_pdf_anywhere() -> None:
    assert read_file_block_reason("docs/report.pdf") is not None


def test_blocks_images_by_default() -> None:
    assert read_file_block_reason("/var/tmp/work/visual/ep01_0-130.jpg") is not None


def test_allows_images_for_image_models() -> None:
    assert read_file_block_reason("/var/tmp/work/visual/ep01_0-130.jpg", allow_images=True) is None
    assert read_file_block_reason("docs/report.pdf", allow_images=True) is not None


def test_blocks_video_and_audio_even_for_image_models() -> None:
    for path in ("/var/tmp/work/clip.mp4", "inbound/01JTEST.MOV", "ep01.mp3", "voice.m4a"):
        reason = read_file_block_reason(path, allow_images=True)
        assert reason is not None
        assert "video/audio" in reason


async def test_guard_follows_the_threads_last_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from langchain_core.messages import ToolMessage

    from octop.infra.agents.middleware import binary_read_guard

    monkeypatch.setattr(binary_read_guard, "_thread_id", lambda: "thr_1")
    guard = BinaryReadGuardMiddleware()
    call = SimpleNamespace(
        tool_call={"name": "read_file", "args": {"file_path": "frame.jpg"}, "id": "c1"}
    )
    read = ToolMessage(content="image", tool_call_id="c1")

    async def tool_handler(_request: Any) -> ToolMessage:
        return read

    async def model_handler(_request: Any) -> str:
        return "ok"

    def model_with(inputs: set[str]) -> Any:
        return SimpleNamespace(model=SimpleNamespace(_harness_supported_input=inputs))

    assert (await guard.awrap_tool_call(call, tool_handler)).status == "error"  # type: ignore[arg-type]
    await guard.awrap_model_call(model_with({"text", "image"}), model_handler)  # type: ignore[arg-type]
    assert await guard.awrap_tool_call(call, tool_handler) is read  # type: ignore[arg-type]
    await guard.awrap_model_call(model_with({"text"}), model_handler)  # type: ignore[arg-type]
    assert (await guard.awrap_tool_call(call, tool_handler)).status == "error"  # type: ignore[arg-type]
