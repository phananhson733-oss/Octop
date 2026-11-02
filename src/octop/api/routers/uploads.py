"""Dashboard chat attachments — stored in agent workspace ``inbound/``."""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile
from pydantic import BaseModel, Field

from octop.api.common.attachments import (
    StoredAttachment,
    dashboard_inbound_preview_url,
    save_attachment_file,
)
from octop.api.common.workspace import require_running_workspace
from octop.api.deps import current_user, get_server
from octop.config import DEFAULT_MAX_UPLOAD_MB, upload_mb_to_bytes
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.media.attachment_hints import sniff_image_media_type
from octop.infra.gateway.media.chunked_upload import ChunkedUpload, chunked_upload_store
from octop.infra.gateway.media.inbound_store import (
    INBOUND_EXTENSION_MEDIA_TYPES,
    validate_inbound_media_type,
)

router = APIRouter()

# Leading bytes read from a stored upload to sniff an image type.
_SNIFF_BYTES = 64


def _resolve_media_type(filename: str, content_type: str | None, data: bytes = b"") -> str:
    raw = (content_type or "").split(";", 1)[0].strip().lower()
    if raw and raw != "application/octet-stream":
        return raw
    ext = Path(filename or "").suffix.lower()
    if ext in INBOUND_EXTENSION_MEDIA_TYPES:
        return INBOUND_EXTENSION_MEDIA_TYPES[ext]
    guessed, _ = mimetypes.guess_type(filename or "")
    if guessed:
        return guessed.lower()
    sniffed = sniff_image_media_type(data)
    if sniffed:
        return sniffed
    return "application/octet-stream"


def _attachment_payload(agent_id: str, stored: StoredAttachment) -> dict[str, str]:
    path = stored.data_path
    preview_url = dashboard_inbound_preview_url(
        agent_id,
        path,
        media_type=stored.media_type,
    )
    return {
        "path": path,
        "workspace_path": path,
        "url": preview_url,
        "access_url": preview_url,
        "filename": stored.filename,
        "media_type": stored.media_type,
    }


def _max_upload_bytes(server: Any) -> int:
    configured = (
        int(server.services.config.max_upload_bytes) if server.services is not None else None
    )
    return configured or upload_mb_to_bytes(DEFAULT_MAX_UPLOAD_MB)


def _read_head(path: Path) -> bytes:
    with path.open("rb") as fh:
        return fh.read(_SNIFF_BYTES)


async def _store_file(
    ws: Any,
    agent_id: str,
    source: Path,
    *,
    owner_id: int,
    filename: str,
    content_type: str | None,
    max_bytes: int,
) -> dict[str, str]:
    head = await asyncio.to_thread(_read_head, source)
    media_type = _resolve_media_type(filename, content_type, head)
    stored = await save_attachment_file(
        ws,
        source,
        owner_id=owner_id,
        filename=filename,
        media_type=media_type,
        max_bytes=max_bytes,
    )
    return _attachment_payload(agent_id, stored)


@router.post("/agents/{agent_id}/upload", summary="Upload a chat attachment")
async def upload_attachment(
    agent_id: str,
    file: UploadFile = File(...),  # noqa: B008
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, str]:
    ws = await require_running_workspace(agent_id, user=user, as_user=as_user, server=server)
    max_bytes = _max_upload_bytes(server)
    source = await chunked_upload_store().spool(file, max_bytes=max_bytes)
    try:
        return await _store_file(
            ws,
            agent_id,
            source,
            owner_id=user.id,
            filename=file.filename or "upload.bin",
            content_type=file.content_type,
            max_bytes=max_bytes,
        )
    finally:
        await asyncio.to_thread(source.unlink, missing_ok=True)


# ----- Chunked upload (large files, each request below proxy body limits) -----


class ChunkedUploadStart(BaseModel):
    filename: str = Field(min_length=1, max_length=1024, description="Original file name.")
    size: int = Field(ge=1, description="Total file size in bytes.")
    content_type: str | None = Field(
        default=None, description="Browser-reported MIME type (``File.type``)."
    )


class ChunkedUploadStatus(BaseModel):
    upload_id: str = Field(description="Session id used in the chunk / complete URLs.")
    filename: str
    size: int = Field(description="Total file size in bytes.")
    chunk_size: int = Field(description="Bytes per chunk; only the last chunk may be shorter.")
    chunk_count: int = Field(description="Number of chunks, indexed from 0.")
    received: list[int] = Field(description="Chunk indexes already stored (for resume).")
    expires_at: float = Field(
        description="Unix time after which an idle session is deleted; each chunk extends it."
    )


class ChunkedUploadChunk(BaseModel):
    index: int
    size: int = Field(description="Bytes stored for this chunk.")


def _status(upload: ChunkedUpload) -> ChunkedUploadStatus:
    store = chunked_upload_store()
    return ChunkedUploadStatus(
        upload_id=upload.upload_id,
        filename=upload.filename,
        size=upload.size,
        chunk_size=upload.chunk_size,
        chunk_count=upload.chunk_count,
        received=store.received(upload),
        expires_at=store.last_activity(upload) + store.ttl_seconds,
    )


async def _load_session(
    agent_id: str, upload_id: str, *, user: Any, as_user: int | None, server: Any
) -> tuple[Any, ChunkedUpload]:
    ws = await require_running_workspace(agent_id, user=user, as_user=as_user, server=server)
    upload = await asyncio.to_thread(
        chunked_upload_store().get, upload_id, agent_id=agent_id, owner_id=user.id
    )
    return ws, upload


@router.post(
    "/agents/{agent_id}/upload/chunked",
    summary="Start a chunked chat attachment upload",
    description=(
        "For files too large for one request (e.g. behind a proxy body limit). "
        "Send each chunk with `PUT …/chunks/{index}`, then `POST …/complete`. "
        "The total size is checked against `max_upload_mb` here. Idle sessions "
        "expire after 24 hours."
    ),
    response_model=ChunkedUploadStatus,
)
async def start_chunked_upload(
    agent_id: str,
    body: ChunkedUploadStart,
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> ChunkedUploadStatus:
    await require_running_workspace(agent_id, user=user, as_user=as_user, server=server)
    media_type = _resolve_media_type(body.filename, body.content_type)
    validate_inbound_media_type(media_type, body.filename)
    upload = await asyncio.to_thread(
        chunked_upload_store().create,
        agent_id=agent_id,
        owner_id=user.id,
        filename=body.filename,
        media_type=body.content_type or "",
        size=body.size,
        max_bytes=_max_upload_bytes(server),
    )
    return await asyncio.to_thread(_status, upload)


@router.get(
    "/agents/{agent_id}/upload/chunked/{upload_id}",
    summary="Chunked upload status",
    description="Lists stored chunk indexes so an interrupted upload can resume.",
    response_model=ChunkedUploadStatus,
)
async def get_chunked_upload(
    agent_id: str,
    upload_id: str,
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> ChunkedUploadStatus:
    _ws, upload = await _load_session(
        agent_id, upload_id, user=user, as_user=as_user, server=server
    )
    return await asyncio.to_thread(_status, upload)


@router.put(
    "/agents/{agent_id}/upload/chunked/{upload_id}/chunks/{index}",
    summary="Upload one chunk",
    description=(
        "Raw request body (`application/octet-stream`) holding bytes "
        "`[index * chunk_size, …)`; it must be exactly the chunk's length. "
        "Re-sending a chunk overwrites it, so failed chunks can simply be retried."
    ),
    response_model=ChunkedUploadChunk,
)
async def put_chunk(
    agent_id: str,
    upload_id: str,
    index: int,
    request: Request,
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> ChunkedUploadChunk:
    _ws, upload = await _load_session(
        agent_id, upload_id, user=user, as_user=as_user, server=server
    )
    expected = upload.chunk_length(index)
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) != expected:
        raise OctopError(
            ErrorCode.UPLOAD_CHUNK_INVALID,
            f"chunk {index} must be {expected} bytes, got {declared}",
            details={"index": index, "expected": expected},
        )
    size = await chunked_upload_store().write_chunk(upload, index, request.stream())
    return ChunkedUploadChunk(index=index, size=size)


@router.post(
    "/agents/{agent_id}/upload/chunked/{upload_id}/complete",
    summary="Finish a chunked upload",
    description=(
        "Moves the assembled file into the agent workspace `inbound/` and returns "
        "the same attachment payload as `POST /agents/{agent_id}/upload`. "
        "Fails with `UPLOAD_INCOMPLETE` while chunks are missing."
    ),
)
async def complete_chunked_upload(
    agent_id: str,
    upload_id: str,
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, str]:
    ws, upload = await _load_session(agent_id, upload_id, user=user, as_user=as_user, server=server)
    store = chunked_upload_store()
    assembled = await store.claim(upload)
    try:
        payload = await _store_file(
            ws,
            agent_id,
            assembled,
            owner_id=user.id,
            filename=upload.filename,
            content_type=upload.media_type,
            max_bytes=_max_upload_bytes(server),
        )
    except BaseException:
        await asyncio.to_thread(store.release, upload, assembled)
        raise
    await asyncio.to_thread(store.discard, upload.upload_id)
    return payload


@router.delete(
    "/agents/{agent_id}/upload/chunked/{upload_id}",
    summary="Cancel a chunked upload",
    status_code=204,
)
async def cancel_chunked_upload(
    agent_id: str,
    upload_id: str,
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> Response:
    _ws, upload = await _load_session(
        agent_id, upload_id, user=user, as_user=as_user, server=server
    )
    await asyncio.to_thread(chunked_upload_store().discard, upload.upload_id)
    return Response(status_code=204)
