"""Block ``read_file`` on binary inbound attachments (PDF, Office, …).

Images pass when the thread's current model reads images: deepagents' ``read_file`` then
returns them as image blocks, so the model can look at frames it extracted itself.
Video and audio never pass: deepagents would inline the whole file as base64, which
overflows the provider's per-item limit and breaks every later turn of the thread.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import ToolMessage
from langgraph.config import get_config
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

logger = logging.getLogger(__name__)

_READ_FILE_TOOL = "read_file"
_BLOCKED_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".zip",
        ".bin",
    }
)
_IMAGE_EXTENSIONS = frozenset({".gif", ".webp", ".jpg", ".jpeg", ".png"})
_MEDIA_EXTENSIONS = frozenset(
    {
        ".mp4",
        ".m4v",
        ".mov",
        ".mkv",
        ".webm",
        ".avi",
        ".flv",
        ".wmv",
        ".mpeg",
        ".mpg",
        ".3gp",
        ".3gpp",
        ".mp3",
        ".m4a",
        ".wav",
        ".aac",
        ".aiff",
        ".ogg",
        ".opus",
        ".flac",
    }
)
_TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".json",
        ".csv",
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".html",
        ".xml",
        ".yaml",
        ".yml",
        ".sql",
        ".sh",
    }
)


def _read_file_path_from_args(params: dict[str, Any]) -> str:
    for key in ("file_path", "path"):
        raw = params.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def _extension(path: str) -> str:
    cleaned = path.split("?", 1)[0].split("#", 1)[0]
    return Path(cleaned).suffix.lower()


def _is_inbound_attachment(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    return normalized.startswith("inbound/") or "/inbound/" in normalized


def _reads_images(model: Any) -> bool:
    # octop-harness records the model's input types on the instance it builds; unknown means no
    supported = getattr(model, "_harness_supported_input", None)
    return isinstance(supported, (set, frozenset)) and "image" in supported


def _thread_id() -> str:
    try:
        configurable = get_config().get("configurable") or {}
    except RuntimeError:  # outside a LangGraph run
        return ""
    return str(configurable.get("thread_id") or "")


def read_file_block_reason(path: str, *, allow_images: bool = False) -> str | None:
    """Return a rejection message when *path* must not be read via ``read_file``."""
    if not path:
        return None
    ext = _extension(path)
    if ext in _MEDIA_EXTENSIONS:
        return (
            f"read_file blocked for video/audio `{path}`: it would put the whole file into the "
            "conversation. Use execute_shell_command with ffprobe/ffmpeg or the clip skill "
            "(extract frames, then read the .jpg frames) instead."
        )
    if allow_images and ext in _IMAGE_EXTENSIONS:
        return None
    if ext == ".pdf":
        return (
            f"read_file blocked for binary PDF `{path}`. "
            "Use execute_shell_command with the pdf skill (pdftotext, pdfplumber, etc.) instead."
        )
    if ext in _BLOCKED_EXTENSIONS or ext in _IMAGE_EXTENSIONS:
        return (
            f"read_file blocked for binary file `{path}`. "
            "Use execute_shell_command or the matching skill instead."
        )
    if _is_inbound_attachment(path) and ext and ext not in _TEXT_EXTENSIONS:
        return (
            f"read_file blocked for inbound attachment `{path}`. "
            "Use execute_shell_command or the matching skill instead."
        )
    return None


class BinaryReadGuardMiddleware(AgentMiddleware[Any, Any]):
    """Reject read_file on PDF/Office inbound attachments before bytes hit context."""

    def __init__(self) -> None:
        super().__init__()
        # Tool calls carry no model; remember per thread whether the last model call read images.
        self._image_threads: dict[str, bool] = {}

    def _note_model(self, request: ModelRequest[Any]) -> None:
        thread_id = _thread_id()
        if thread_id:
            self._image_threads[thread_id] = _reads_images(request.model)

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        self._note_model(request)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        self._note_model(request)
        return await handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        tool_call = request.tool_call
        tool_name = str(tool_call.get("name") or "")
        if tool_name != _READ_FILE_TOOL:
            return await handler(request)

        params = tool_call.get("args") or {}
        if not isinstance(params, dict):
            params = {}
        path = _read_file_path_from_args(params)
        allow_images = self._image_threads.get(_thread_id(), False)
        reason = read_file_block_reason(path, allow_images=allow_images)
        if reason is None:
            return await handler(request)

        logger.info("BinaryReadGuard blocked read_file on %s", path)
        return ToolMessage(
            content=reason,
            tool_call_id=str(tool_call.get("id") or ""),
            status="error",
        )


__all__ = ["BinaryReadGuardMiddleware", "read_file_block_reason"]
