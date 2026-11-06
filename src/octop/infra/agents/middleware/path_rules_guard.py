"""Close gaps in harness FilesystemGuard's path rules.

* ``send_file_to_user`` is not a file tool to the guard, so a denied path could still
  be sent to the user as an attachment. Its path is resolved (links, ``..``) and
  checked as a read; the guard itself lets a path with ``..`` through unchecked.
* ``ls`` / ``glob`` / ``grep`` without a ``path`` search the backend root, and the
  guard only checks a path that is given. They are checked as ``/`` here.
* With ``OCTOP_FS_CONFINE_WRITES=1`` (customer instances), file-tool writes must land
  in the workspace, the temp dir or ``OCTOP_SHELL_WRITABLE_DIRS`` once links are
  resolved, and ``task`` is refused: subagents run without this middleware stack.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from deepagents.backends.utils import validate_path
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from octop_harness.middleware.filesystem_guard import filesystem_guard_block_reason

from octop.infra.utils.write_roots import (
    backend_host_paths,
    path_shape_ok,
    write_roots,
    write_target_allowed,
)

logger = logging.getLogger(__name__)

_CONFINE_ENV = "OCTOP_FS_CONFINE_WRITES"
_SEND_FILE_TOOL = "send_file_to_user"
_SEARCH_TOOLS = frozenset({"ls", "glob", "grep"})
_WRITE_TOOLS = frozenset(
    {"write_file", "edit_file", "append_file", "delete", "delete_file", "move_file"}
)
_TASK_TOOL = "task"


def confine_writes_from_env() -> bool:
    return os.environ.get(_CONFINE_ENV, "").strip() == "1"


class PathRulesGuardMiddleware(AgentMiddleware[Any, Any]):
    """Refuse the calls above (see module docstring)."""

    def __init__(
        self,
        permissions: list[Any],
        *,
        workspace_dir: Path | str,
        confine_writes: bool = False,
    ) -> None:
        super().__init__()
        self._permissions = permissions
        self._workspace = Path(workspace_dir)
        self._confine_writes = confine_writes

    def _read_reason(self, name: str, params: dict[str, Any]) -> str | None:
        if not self._permissions:
            return None
        return filesystem_guard_block_reason(self._permissions, tool_name=name, params=params)

    def _reason(self, name: str, args: dict[str, Any]) -> str | None:
        if name == _SEND_FILE_TOOL:
            # Normalized like BackendWorkspace reads it; a relative path is checked under
            # both the workspace and the backend root ``/``.
            raw = str(args.get("file_path") or "")
            if self._confine_writes and not path_shape_ok(raw):
                return f"Error: send the file by a plain path, not {raw!r}."
            rel = raw.strip().replace("\\", "/")
            hosts = backend_host_paths(raw)
            candidates = hosts or [str(self._workspace / rel), str(Path("/") / rel)]
            for candidate in candidates:
                resolved = os.path.realpath(candidate)
                if reason := self._read_reason("read_file", {"file_path": resolved}):
                    return reason
            if self._confine_writes:
                # Only what the agent made or was given: workspace, temp, clip dirs.
                roots = write_roots(self._workspace)
                sent = hosts or [str(self._workspace / rel)]
                if not all(write_target_allowed(p, roots) for p in sent):
                    return f"Error: sending {raw!r} is not allowed on this instance."
            return None
        if name in _SEARCH_TOOLS and not str(args.get("path") or "").strip():
            return self._read_reason(name, {**args, "path": "/"})
        if not self._confine_writes:
            return None
        if name == _TASK_TOOL:
            return "Error: subagents are turned off on this instance."
        if name in _WRITE_TOOLS:
            roots = write_roots(self._workspace)
            targets = [
                v
                for k, v in args.items()
                if isinstance(v, str) and k.endswith(("path", "source", "destination"))
            ]
            for target in targets:
                # Check the path the tool opens: deepagents' validate_path makes a relative
                # path ``/path`` under the backend root ``/``, and keeps trailing spaces.
                try:
                    opened = validate_path(target)
                except ValueError:
                    opened = None
                if opened is None or not write_target_allowed(opened, roots):
                    return f"Error: writing to {target!r} is not allowed on this instance."
        return None

    def _blocked(self, request: ToolCallRequest) -> ToolMessage | None:
        tool_call = request.tool_call
        raw_args = tool_call.get("args")
        args = raw_args if isinstance(raw_args, dict) else {}
        name = str(tool_call.get("name") or "")
        reason = self._reason(name, args)
        if reason is None:
            return None
        logger.info("PathRulesGuard blocked %s: %s", name, reason)
        return ToolMessage(
            content=reason, tool_call_id=str(tool_call.get("id") or ""), status="error"
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        return self._blocked(request) or handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        return self._blocked(request) or await handler(request)


__all__ = ["PathRulesGuardMiddleware", "confine_writes_from_env"]
