"""Workspace router — read/write into a running agent's workspace."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from octop_harness.backends.utils import BackendOperationNotSupportedError
from pydantic import BaseModel, Field

from octop.api.common.agent_workspace import resolve_agent_workspace_dir
from octop.api.common.content_disposition import content_disposition
from octop.api.common.workspace import (
    coerce_read_content,
    file_info_to_dict,
    reanchor_entry_path,
    require_agent_workspace,
    require_running_workspace,
    workspace_api_path,
)
from octop.api.deps import current_user, get_server
from octop.infra.agents.providers.codex_image_tool import generate_to_workspace
from octop.infra.agents.settings.chatgpt_image import may_use_chatgpt_image
from octop.infra.backend.tree_listing import dedupe_tree_rows
from octop.infra.backup.workspace_archive import export_workspace_zip, import_workspace_zip
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.media.backend_files import (
    backend_workspace_path,
    host_path_forms,
    is_admin_only_host_path,
    is_allowed_host_download_abs_path,
    is_host_absolute_path,
    resolve_preview_file,
    resolve_preview_payload,
)
from octop.infra.utils.doc_edit import DocConverter, get_doc_converter

logger = logging.getLogger(__name__)

_PROTECTED_PREFIX = "_builtin_skills"


def _assert_workspace_mutable(path: str) -> str:
    """Mutating ops always treat paths as workspace-relative (``from_workspace=true``)."""
    rel = _workspace_io_path(path, from_workspace=True)
    if rel == ".":
        raise OctopError(ErrorCode.FORBIDDEN, "cannot modify workspace root")
    posix = rel.replace("\\", "/").strip("/")
    if (
        posix == _PROTECTED_PREFIX
        or posix.startswith(f"{_PROTECTED_PREFIX}/")
        or posix == f".octop/{_PROTECTED_PREFIX}"
        or posix.startswith(f".octop/{_PROTECTED_PREFIX}/")
    ):
        raise OctopError(ErrorCode.FORBIDDEN, f"cannot modify {_PROTECTED_PREFIX!r} paths")
    return rel


def _map_workspace_fs_error(exc: Exception, *, operation: str, path: str) -> OctopError:
    if isinstance(exc, BackendOperationNotSupportedError):
        return OctopError(ErrorCode.WORKSPACE_OP_UNSUPPORTED, str(exc))
    if isinstance(exc, FileNotFoundError):
        return OctopError(ErrorCode.NOT_FOUND, f"cannot {operation} {path!r}: not found")
    if isinstance(exc, FileExistsError):
        return OctopError(ErrorCode.SLASH_BAD_ARGS, str(exc))
    if isinstance(exc, PermissionError):
        return OctopError(ErrorCode.FORBIDDEN, str(exc))
    if isinstance(exc, ValueError):
        return OctopError(ErrorCode.SLASH_BAD_ARGS, str(exc))
    return OctopError(ErrorCode.INTERNAL_ERROR, f"cannot {operation} {path!r}: {exc}")


def _ensure_editable_doc(path: str) -> DocConverter:
    """Return the registered converter for *path* or reject with a 400."""
    converter = get_doc_converter(path)
    if converter is None:
        raise OctopError(
            ErrorCode.SLASH_BAD_ARGS,
            f"unsupported editable document {path!r}",
        )
    return converter


def _agent_id_from_media_source(source: str) -> str | None:
    match = re.search(r"/agents/([A-Z0-9]+)/", source, re.IGNORECASE)
    return match.group(1) if match else None


def _workspace_io_path(path: str, *, from_workspace: bool = False) -> str:
    """Resolve an API path for ``BackendWorkspace``.

    ``file://`` is always a host absolute path.

    When ``from_workspace`` is true (workspace UI): leading ``/`` is relative to
    the agent workspace dir (``/logo.png`` → ``logo.png``).

    When false (default, chat/tool downloads): leading ``/`` is a host
    filesystem absolute (``/Users/…``, ``/root/…``). Paths without a leading
    ``/`` stay workspace-relative (``outbound/a.pptx``).
    """
    raw = path.strip()
    if raw.startswith("file://"):
        resolved = backend_workspace_path(raw)
        if resolved is None:
            raise OctopError(ErrorCode.NOT_FOUND, f"cannot resolve {path!r}")
        return resolved
    if from_workspace:
        return workspace_api_path(raw)
    if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":") or raw.startswith("\\\\"):
        return raw
    return workspace_api_path(raw)


def _guard_host_path(io_path: str, ws: Any, user: Any) -> str:
    """Refuse a non-admin any host path only admins may open.

    Agents' files may sit anywhere on the host, so these routes take host paths;
    Octop's own data (config, env, other agents' workspaces) and OS system roots
    stay admin-only. See ``is_admin_only_host_path``.
    """
    if not user.is_admin and any(
        is_admin_only_host_path(host, workspace=ws.workspace_dir)
        for host in host_path_forms(io_path)
    ):
        raise OctopError(ErrorCode.FORBIDDEN, f"path not allowed: {io_path!r}")
    return io_path


def _drop_admin_only_rows(rows: list[dict[str, Any]], ws: Any, user: Any) -> list[dict[str, Any]]:
    """Listing and search results without the host paths a non-admin may not open."""
    if user.is_admin:
        return rows
    return [
        row
        for row in rows
        if not any(
            is_admin_only_host_path(host, workspace=ws.workspace_dir)
            for host in host_path_forms(str(row.get("path") or ""))
        )
    ]


_FROM_WORKSPACE_DESC = (
    "When true, leading '/' paths are workspace-relative (workspace UI). "
    "When false (default), leading '/' is host-absolute."
)


router = APIRouter()


@router.get("/agents/{agent_id}/workspace/tree")
async def list_tree(
    agent_id: str,
    path: str = "/",
    from_workspace: bool = Query(default=False, description=_FROM_WORKSPACE_DESC),
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    """Single-level directory listing under ``path`` (agent must be running)."""
    ws = await require_running_workspace(
        agent_id, user=user, as_user=as_user, server=server, owner_only=True
    )
    io_path = _guard_host_path(_workspace_io_path(path, from_workspace=from_workspace), ws, user)
    result = await ws.als(io_path)
    if result is None:
        raise OctopError(ErrorCode.NOT_FOUND, f"cannot list {path!r}")
    entries = getattr(result, "entries", None) or []
    rows = _drop_admin_only_rows([file_info_to_dict(f) for f in entries], ws, user)
    if not is_host_absolute_path(io_path):
        for row in rows:
            row["path"] = reanchor_entry_path(str(row.get("path") or ""), parent=io_path)
    return dedupe_tree_rows(rows, parent=io_path)


class WriteFileBody(BaseModel):
    content: str
    """UTF-8 text content. Use ``/upload`` for binary."""


class WriteDocBody(BaseModel):
    content: str
    """Markdown content to convert back into the document format."""


@router.get("/agents/{agent_id}/workspace/file")
async def read_file(
    agent_id: str,
    path: str,
    from_workspace: bool = Query(default=False, description=_FROM_WORKSPACE_DESC),
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Read a UTF-8 text file."""
    ws = await require_agent_workspace(agent_id, user=user, as_user=as_user, server=server)
    io_path = _guard_host_path(_workspace_io_path(path, from_workspace=from_workspace), ws, user)
    content = await ws.aread_text(io_path)
    if content is None:
        raise OctopError(ErrorCode.NOT_FOUND, f"cannot read {path!r}")
    return {"path": path, "content": coerce_read_content(content)}


@router.put("/agents/{agent_id}/workspace/file")
async def write_file(
    agent_id: str,
    body: WriteFileBody,
    path: str,
    from_workspace: bool = Query(default=False, description=_FROM_WORKSPACE_DESC),
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Overwrite ``path`` with ``body.content`` (text)."""
    ws = await require_running_workspace(
        agent_id, user=user, as_user=as_user, server=server, owner_only=True
    )
    converter = get_doc_converter(path)
    if converter is not None:
        # Editable-document paths are always stored as the binary document
        # format. This matters for workspace "new file": an empty .docx created
        # here must be a valid package so its preview (and edit round-trip)
        # works immediately instead of showing a 0-byte file.
        try:
            data = converter.from_markdown(body.content)
        except Exception as exc:
            raise OctopError(
                ErrorCode.SLASH_BAD_ARGS,
                f"cannot build document for {path!r}: {exc}",
            ) from exc
    else:
        data = body.content.encode("utf-8")
    io_path = _guard_host_path(_workspace_io_path(path, from_workspace=from_workspace), ws, user)
    try:
        await ws.aupload_bytes(io_path, data)
    except Exception as exc:
        raise OctopError(ErrorCode.NOT_FOUND, f"cannot write {path!r}: {exc}") from exc
    return {"path": path, "size": len(data)}


class MoveFileBody(BaseModel):
    destination: str
    """Workspace-relative destination path (e.g. ``/sub/file.txt``)."""


@router.post(
    "/agents/{agent_id}/workspace/mkdir",
    status_code=201,
    summary="Create workspace directory",
)
async def mkdir_workspace_dir(
    agent_id: str,
    path: str,
    from_workspace: bool = Query(
        default=True,
        description="Mutating endpoints always treat paths as workspace-relative.",
    ),
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Create a directory (and parents) under the agent workspace."""
    _ = from_workspace  # API surface; mutations always use workspace-relative paths.
    rel = _assert_workspace_mutable(path)
    ws = await require_running_workspace(
        agent_id, user=user, as_user=as_user, server=server, owner_only=True
    )
    _guard_host_path(rel, ws, user)
    try:
        await ws.amkdir(rel)
    except Exception as exc:
        raise _map_workspace_fs_error(exc, operation="mkdir", path=path) from exc
    api_path = path if path.startswith("/") else f"/{path}"
    return {"path": api_path, "is_dir": True}


@router.delete(
    "/agents/{agent_id}/workspace/file",
    status_code=204,
    summary="Delete workspace file or directory",
)
async def delete_workspace_file(
    agent_id: str,
    path: str,
    from_workspace: bool = Query(
        default=True,
        description="Mutating endpoints always treat paths as workspace-relative.",
    ),
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> Response:
    """Remove a file or directory tree from the agent workspace."""
    _ = from_workspace
    rel = _assert_workspace_mutable(path)
    ws = await require_running_workspace(
        agent_id, user=user, as_user=as_user, server=server, owner_only=True
    )
    _guard_host_path(rel, ws, user)
    try:
        await ws.adelete(rel)
    except Exception as exc:
        raise _map_workspace_fs_error(exc, operation="delete", path=path) from exc
    return Response(status_code=204)


@router.post(
    "/agents/{agent_id}/workspace/move",
    summary="Move or rename a workspace file or directory",
)
async def move_workspace_file(
    agent_id: str,
    body: MoveFileBody,
    path: str,
    from_workspace: bool = Query(
        default=True,
        description="Mutating endpoints always treat paths as workspace-relative.",
    ),
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Move ``path`` to ``body.destination`` (rename when the parent directory is unchanged)."""
    _ = from_workspace
    src = _assert_workspace_mutable(path)
    dest = _assert_workspace_mutable(body.destination)
    ws = await require_running_workspace(
        agent_id, user=user, as_user=as_user, server=server, owner_only=True
    )
    _guard_host_path(src, ws, user)
    _guard_host_path(dest, ws, user)
    try:
        await ws.amove(src, dest)
    except Exception as exc:
        raise _map_workspace_fs_error(exc, operation="move", path=path) from exc
    dest_api = body.destination if body.destination.startswith("/") else f"/{body.destination}"
    return {"path": dest_api}


@router.post("/agents/{agent_id}/workspace/upload")
async def upload_file(
    agent_id: str,
    file: UploadFile = File(...),  # noqa: B008
    path: str | None = Query(default=None),
    from_workspace: bool = Query(default=False, description=_FROM_WORKSPACE_DESC),
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Upload a binary file via multipart ``file=@...``."""
    ws = await require_running_workspace(
        agent_id, user=user, as_user=as_user, server=server, owner_only=True
    )
    target = path or f"/{file.filename or 'upload.bin'}"
    data = await file.read()
    io_path = _guard_host_path(_workspace_io_path(target, from_workspace=from_workspace), ws, user)
    try:
        await ws.aupload_bytes(io_path, data)
    except Exception as exc:
        raise OctopError(ErrorCode.NOT_FOUND, f"cannot upload to {target!r}: {exc}") from exc
    return {"path": target, "size": len(data)}


@router.get(
    "/agents/{agent_id}/workspace/download",
    summary="Download a workspace file",
    description="Authenticated file download. Materialized files support HTTP byte ranges without buffering the whole file.",
    responses={
        206: {"description": "Requested byte range"},
        416: {"description": "Invalid byte range"},
    },
)
async def download_file(
    agent_id: str,
    path: str,
    from_workspace: bool = Query(default=False, description=_FROM_WORKSPACE_DESC),
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> Response:
    """Stream ``path`` back as application/octet-stream.

    See ``from_workspace``: workspace UI uses true; chat/tool downloads use false.
    ``file://`` and other host-absolute paths are allowed for agent/OS tool
    outputs (Desktop, ``~/.octop/agents/…``, workspace tree) but denied for
    sensitive system roots (``/etc``, ``.harness-browser``, ``.octop-browser``, Windows system dirs).
    """
    ws = await require_agent_workspace(agent_id, user=user, as_user=as_user, server=server)
    io_path = _guard_host_path(_workspace_io_path(path, from_workspace=from_workspace), ws, user)
    if is_host_absolute_path(io_path) and not is_allowed_host_download_abs_path(
        io_path,
        workspace=ws.workspace_dir,
    ):
        raise OctopError(ErrorCode.FORBIDDEN, f"cannot download {path!r}: path not allowed")

    try:
        local = await asyncio.to_thread(ws.materialize_local, io_path)
    except PermissionError as exc:
        raise OctopError(ErrorCode.NOT_FOUND, f"cannot download {path!r}") from exc
    if local is not None:
        if not is_allowed_host_download_abs_path(str(local), workspace=ws.workspace_dir):
            raise OctopError(ErrorCode.FORBIDDEN, "download path not allowed")
        _guard_host_path(str(local), ws, user)
        return FileResponse(
            local,
            media_type="application/octet-stream",
            headers={"Content-Disposition": content_disposition(io_path.rsplit("/", 1)[-1])},
        )
    try:
        file_blob = await ws.adownload_bytes(io_path)
    except PermissionError as exc:
        raise OctopError(ErrorCode.NOT_FOUND, f"cannot download {path!r}") from exc
    if file_blob is None:
        raise OctopError(ErrorCode.NOT_FOUND, f"cannot download {path!r}") from None

    fname = io_path.rsplit("/", 1)[-1] or "download.bin"
    return StreamingResponse(
        iter([file_blob]),
        media_type="application/octet-stream",
        headers={"Content-Disposition": content_disposition(fname)},
    )


@router.get("/agents/{agent_id}/workspace/doc")
async def read_doc(
    agent_id: str,
    path: str,
    from_workspace: bool = Query(default=False, description=_FROM_WORKSPACE_DESC),
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Read an editable document (e.g. ``.docx``) as Markdown for online editing."""
    converter = _ensure_editable_doc(path)
    ws = await require_agent_workspace(agent_id, user=user, as_user=as_user, server=server)
    io_path = _guard_host_path(_workspace_io_path(path, from_workspace=from_workspace), ws, user)
    try:
        blob = await ws.adownload_bytes(io_path)
    except PermissionError as exc:
        raise OctopError(ErrorCode.NOT_FOUND, f"cannot read {path!r}") from exc
    if blob is None:
        raise OctopError(ErrorCode.NOT_FOUND, f"cannot read {path!r}")
    try:
        content = converter.to_markdown(blob)
    except Exception as exc:
        raise OctopError(ErrorCode.SLASH_BAD_ARGS, f"cannot parse {path!r}: {exc}") from exc
    return {"path": path, "content": content}


@router.put("/agents/{agent_id}/workspace/doc")
async def write_doc(
    agent_id: str,
    body: WriteDocBody,
    path: str,
    from_workspace: bool = Query(
        default=True,
        description="Mutating endpoints always treat paths as workspace-relative.",
    ),
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Convert Markdown *content* back to the document format and overwrite *path*."""
    _ = from_workspace  # Mutations always use workspace-relative paths.
    rel = _assert_workspace_mutable(path)
    converter = _ensure_editable_doc(path)
    ws = await require_running_workspace(
        agent_id, user=user, as_user=as_user, server=server, owner_only=True
    )
    try:
        data = converter.from_markdown(body.content)
    except Exception as exc:
        raise OctopError(
            ErrorCode.SLASH_BAD_ARGS,
            f"cannot build document for {path!r}: {exc}",
        ) from exc
    _guard_host_path(rel, ws, user)
    try:
        await ws.aupload_bytes(rel, data)
    except Exception as exc:
        raise _map_workspace_fs_error(exc, operation="write", path=path) from exc
    return {"path": path, "size": len(data)}


@router.get(
    "/agents/{agent_id}/media/preview",
    summary="Preview image or video",
    description="Authenticated sandboxed media preview. Materialized files support HTTP byte ranges.",
    responses={
        206: {"description": "Requested byte range"},
        416: {"description": "Invalid byte range"},
    },
    response_class=StreamingResponse,
)
async def preview_media(
    agent_id: str,
    source: str = Query(..., description="``file://`` URL or workspace-relative path"),
    mime_type: str | None = Query(default=None, alias="mime_type"),
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> Response:
    """Stream an image or video inline for dashboard tool-result previews."""
    path_agent = _agent_id_from_media_source(source)
    effective_agent = path_agent or agent_id
    ws = await require_running_workspace(effective_agent, user=user, as_user=as_user, server=server)
    _guard_host_path(source, ws, user)
    local = await resolve_preview_file(source=source, workspace=ws, mime_hint=mime_type or "")
    if local is not None:
        path, mime = local
        _guard_host_path(str(path), ws, user)
        return FileResponse(
            path,
            media_type=mime,
            headers={
                "Content-Disposition": "inline",
                "Content-Security-Policy": "sandbox",
                "X-Content-Type-Options": "nosniff",
            },
        )
    payload = await resolve_preview_payload(
        source=source,
        workspace=ws,
        mime_hint=mime_type or "",
    )
    if payload is None:
        raise OctopError(ErrorCode.NOT_FOUND, "preview not available for this source")
    data, mime = payload

    # The source bytes are user-controlled (uploads, tool outputs). Serving them
    # inline without a sandbox CSP would let a navigated SVG (any image/* type)
    # run scripts on this origin; "sandbox" keeps image/video previews working
    # while disabling script execution in the document itself.
    return StreamingResponse(
        iter([data]),
        media_type=mime,
        headers={
            "Content-Disposition": "inline",
            "Content-Security-Policy": "sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )


class ChatgptImageBody(BaseModel):
    prompt: str = Field(description="Full description of the image to create.")
    aspect_ratio: Literal["square", "landscape", "portrait"] = Field(
        default="square",
        description="square (1024x1024), landscape (1536x1024) or portrait (1024x1536).",
    )
    quality: Literal["low", "medium", "high"] = Field(
        default="medium",
        description="low is fastest and uses the least allowance; high is slowest.",
    )


class ChatgptImageFile(BaseModel):
    path: str = Field(description="Workspace-relative path of the saved PNG.")
    mediaType: str  # noqa: N815 - same key as the tool envelope
    sizeBytes: int  # noqa: N815


class ChatgptImageError(BaseModel):
    code: str = Field(
        description=(
            "disabled, admin_only, auth_required, quota_exhausted, quota_unknown, "
            "invalid_request, unsupported_model, rate_limited or another failure code."
        )
    )
    message: str
    retryable: bool
    safe_to_resubmit: bool = Field(description="True when nothing was sent to ChatGPT.")


class ChatgptImageResult(BaseModel):
    status: Literal["completed", "failed"]
    message: str
    prompt: str
    images: list[ChatgptImageFile]
    error: ChatgptImageError | None
    execution: dict[str, Any] | None


@router.post(
    "/agents/{agent_id}/media/chatgpt-image",
    summary="Generate an image into the agent workspace",
    description=(
        "Same as the agent's chatgpt_image tool, called from the dashboard: draws one PNG "
        "with the instance's ChatGPT login and saves it under generated/image/ in this "
        "agent's workspace. The same switches apply (turned on, admin only, allowance "
        "check unless credits are allowed). A refused or failed generation is still a 200 "
        "with status 'failed' and an error code. Owner only."
    ),
    response_model=ChatgptImageResult,
)
async def generate_chatgpt_image(
    agent_id: str,
    body: ChatgptImageBody,
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> ChatgptImageResult:
    ws = await require_running_workspace(
        agent_id, user=user, as_user=as_user, server=server, owner_only=True
    )
    result = await generate_to_workspace(
        workspace=ws,
        paths=server.services.paths,
        provider_repo=server.services.provider_repo,
        settings_store=server.app_runtime.agent_registry.chatgpt_image,
        prompt=body.prompt,
        aspect_ratio=body.aspect_ratio,
        quality=body.quality,
        acting_user_may_use=may_use_chatgpt_image(
            is_admin=bool(getattr(user, "is_admin", False)),
            permissions=getattr(user, "permissions", None),
        ),
    )
    return ChatgptImageResult.model_validate(result)


@router.get("/agents/{agent_id}/workspace/glob")
async def glob_files(
    agent_id: str,
    pattern: str,
    path: str = "/",
    from_workspace: bool = Query(default=False, description=_FROM_WORKSPACE_DESC),
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    ws = await require_running_workspace(
        agent_id, user=user, as_user=as_user, server=server, owner_only=True
    )
    root = _guard_host_path(_workspace_io_path(path, from_workspace=from_workspace), ws, user)
    _guard_host_path(pattern, ws, user)
    if pattern in ("**/*.md", "*.md") and root == ".":
        ls_result = await ws.als(".")
        if ls_result is None:
            raise OctopError(ErrorCode.NOT_FOUND, "glob failed")
        entries = getattr(ls_result, "entries", None) or []
        matches = []
        for f in entries:
            row = file_info_to_dict(f)
            if row.get("is_dir"):
                continue
            entry_path = str(row.get("path") or "")
            if entry_path.endswith(".md"):
                matches.append(row)
        return matches
    glob_result = await ws.aglob(pattern, root)
    if glob_result is None:
        raise OctopError(ErrorCode.NOT_FOUND, "glob failed")
    matches = getattr(glob_result, "matches", None) or []
    return _drop_admin_only_rows([file_info_to_dict(item) for item in matches], ws, user)


@router.get("/agents/{agent_id}/workspace/grep")
async def grep_files(
    agent_id: str,
    pattern: str,
    path: str = "/",
    from_workspace: bool = Query(default=False, description=_FROM_WORKSPACE_DESC),
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    ws = await require_running_workspace(
        agent_id, user=user, as_user=as_user, server=server, owner_only=True
    )
    root = _guard_host_path(_workspace_io_path(path, from_workspace=from_workspace), ws, user)
    result = await ws.agrep(pattern, root)
    if result is None:
        raise OctopError(ErrorCode.NOT_FOUND, "grep failed")
    matches = getattr(result, "matches", None) or []
    return _drop_admin_only_rows([dict(m) for m in matches], ws, user)


_MAX_WORKSPACE_ARCHIVE_BYTES = 200 * 1024 * 1024


@router.get(
    "/agents/{agent_id}/workspace/archive",
    summary="Download workspace as zip",
    response_class=StreamingResponse,
)
async def export_workspace_archive(
    agent_id: str,
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> StreamingResponse:
    """Pack workspace files into a zip archive."""
    ws = await require_running_workspace(
        agent_id, user=user, as_user=as_user, server=server, owner_only=True
    )
    data = await export_workspace_zip(ws)
    filename = f"workspace-{agent_id}.zip"
    return StreamingResponse(
        iter([data]),
        media_type="application/zip",
        headers={"Content-Disposition": content_disposition(filename)},
    )


@router.post(
    "/agents/{agent_id}/workspace/archive",
    summary="Import workspace zip",
)
async def import_workspace_archive(
    agent_id: str,
    file: UploadFile = File(...),  # noqa: B008
    mode: Literal["merge", "replace"] = Query(default="merge"),
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Import a zip archive into the workspace (merge or replace)."""
    raw = await file.read()
    if len(raw) > _MAX_WORKSPACE_ARCHIVE_BYTES:
        raise OctopError(ErrorCode.SLASH_BAD_ARGS, "workspace archive too large (max 200MB)")
    if not raw:
        raise OctopError(ErrorCode.SLASH_BAD_ARGS, "empty archive")

    ws = await require_running_workspace(
        agent_id, user=user, as_user=as_user, server=server, owner_only=True
    )
    local_ws = resolve_agent_workspace_dir(server, agent_id)
    result = await import_workspace_zip(
        ws,
        raw,
        mode=mode,
        local_workspace_dir=local_ws,
    )
    return {"ok": True, **result}
