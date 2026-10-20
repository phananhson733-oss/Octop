"""Unit tests for WorkspaceImageMaterializeMiddleware."""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from deepagents.backends.local_shell import LocalShellBackend
from langchain_core.messages import HumanMessage
from octop_harness.backends.workspace import BackendWorkspace

from octop.api.common.attachments import save_attachment
from octop.infra.agents.middleware.workspace_image import WorkspaceImageMaterializeMiddleware
from octop.infra.gateway.media.attachment_hints import make_workspace_image_ref

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.mark.asyncio
async def test_awrap_model_call_expands_workspace_image_ref() -> None:
    with tempfile.TemporaryDirectory() as ws_dir:
        workspace = BackendWorkspace(
            LocalShellBackend(root_dir=ws_dir, virtual_mode=False),
            ws_dir,
        )
        stored = await save_attachment(
            workspace,
            owner_id=1,
            filename="shot.png",
            media_type="image/png",
            data=_PNG,
        )
        ref = make_workspace_image_ref(workspace_path=stored.data_path, mime_type="image/png")
        human = HumanMessage(content=[{"type": "text", "text": "see"}, ref])
        captured: dict[str, Any] = {}

        async def handler(request: Any) -> Any:
            captured["messages"] = request.messages
            return MagicMock()

        request = MagicMock()
        request.messages = [human]
        request.override = lambda **kw: MagicMock(messages=kw["messages"])

        mw = WorkspaceImageMaterializeMiddleware(workspace=workspace)
        await mw.awrap_model_call(request, handler)

        expanded = captured["messages"][0].content[1]
        url = expanded["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        assert base64.b64decode(url.split(",", 1)[1]) == _PNG
        # Original checkpoint message is unchanged.
        assert human.content[1]["image_url"]["url"].startswith("workspace://")


@pytest.mark.asyncio
async def test_awrap_model_call_gives_a_text_only_model_the_path() -> None:
    with tempfile.TemporaryDirectory() as ws_dir:
        workspace = BackendWorkspace(
            LocalShellBackend(root_dir=ws_dir, virtual_mode=False),
            ws_dir,
        )
        stored = await save_attachment(
            workspace,
            owner_id=1,
            filename="shot.png",
            media_type="image/png",
            data=_PNG,
        )
        ref = make_workspace_image_ref(workspace_path=stored.data_path, mime_type="image/png")
        human = HumanMessage(content=[{"type": "text", "text": "see"}, ref])
        captured: dict[str, Any] = {}

        async def handler(request: Any) -> Any:
            captured["messages"] = request.messages
            return MagicMock()

        request = MagicMock()
        request.messages = [human]
        request.model._harness_supported_input = {"text"}
        request.override = lambda **kw: MagicMock(messages=kw["messages"])

        mw = WorkspaceImageMaterializeMiddleware(workspace=workspace)
        await mw.awrap_model_call(request, handler)

        hint = captured["messages"][0].content[1]
        assert hint["type"] == "text"
        assert "shot.png" in hint["text"]
        assert "base64" not in hint["text"]
        # Original checkpoint message is unchanged.
        assert human.content[1]["image_url"]["url"].startswith("workspace://")


def test_wrap_model_call_keeps_images_for_a_model_that_reads_them() -> None:
    with tempfile.TemporaryDirectory() as ws_dir:
        workspace = BackendWorkspace(
            LocalShellBackend(root_dir=ws_dir, virtual_mode=False),
            ws_dir,
        )
        (Path(ws_dir) / "inbound").mkdir()
        (Path(ws_dir) / "inbound" / "a.png").write_bytes(_PNG)
        ref = make_workspace_image_ref(workspace_path="inbound/a.png", mime_type="image/png")
        captured: dict[str, Any] = {}

        def handler(request: Any) -> Any:
            captured["messages"] = request.messages
            return MagicMock()

        request = MagicMock()
        request.messages = [HumanMessage(content=[{"type": "text", "text": "see"}, ref])]
        request.model._harness_supported_input = {"text", "image"}
        request.override = lambda **kw: MagicMock(messages=kw["messages"])

        WorkspaceImageMaterializeMiddleware(workspace=workspace).wrap_model_call(request, handler)

        assert captured["messages"][0].content[1]["image_url"]["url"].startswith("data:image/png")
