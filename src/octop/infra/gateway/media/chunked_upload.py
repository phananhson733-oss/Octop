"""Chunked chat uploads — large attachments sent as several small requests.

Each session lives in ``{root}/{upload_id}/``:

* ``manifest.json`` — owner, agent, filename, size; its mtime is the last activity.
* ``data.bin`` — sparse file of the final size; every chunk is written at its
  offset, so chunks may arrive in any order and be re-sent.
* ``chunks/{index}`` — empty marker written after a chunk was fully stored.

Bytes stream from the request straight to disk; nothing holds a whole chunk or
file in memory. Sessions idle longer than the TTL are removed by
:meth:`ChunkedUploadStore.sweep_expired`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.paths import PathLayout
from octop.infra.utils.ulid import new_ulid

CHUNK_BYTES = 32 * 1024 * 1024
SESSION_TTL_SECONDS = 24 * 60 * 60

_WRITE_BUFFER = 1024 * 1024
_UPLOAD_ID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_MANIFEST = "manifest.json"
_DATA = "data.bin"
_ASSEMBLED = "assembled.bin"
_CHUNKS = "chunks"


class AsyncByteReader(Protocol):
    async def read(self, size: int = -1) -> bytes: ...


@dataclass(frozen=True)
class ChunkedUpload:
    upload_id: str
    agent_id: str
    owner_id: int
    filename: str
    media_type: str
    size: int
    chunk_size: int
    created_at: float

    @property
    def chunk_count(self) -> int:
        return -(-self.size // self.chunk_size)

    def chunk_length(self, index: int) -> int:
        if index < 0 or index >= self.chunk_count:
            raise OctopError(
                ErrorCode.UPLOAD_CHUNK_INVALID,
                f"chunk index {index} out of range 0..{self.chunk_count - 1}",
            )
        return min(self.chunk_size, self.size - index * self.chunk_size)


def _too_large(max_bytes: int) -> OctopError:
    max_mb = max(1, max_bytes // (1024 * 1024))
    return OctopError(
        ErrorCode.ATTACHMENT_TOO_LARGE,
        f"file too large (max {max_mb}MB)",
        details={"max_mb": max_mb},
    )


def _not_found(upload_id: str) -> OctopError:
    return OctopError(ErrorCode.UPLOAD_SESSION_NOT_FOUND, f"upload {upload_id!r} not found")


class ChunkedUploadStore:
    """Disk-backed chunked upload sessions under *root*."""

    def __init__(
        self,
        root: Path,
        *,
        chunk_size: int = CHUNK_BYTES,
        ttl_seconds: float = SESSION_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._root = root
        self._chunk_size = chunk_size
        self._ttl = ttl_seconds
        self._clock = clock

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def _dir(self, upload_id: str) -> Path:
        if not _UPLOAD_ID.match(upload_id):
            raise _not_found(upload_id)
        return self._root / upload_id

    def _expired(self, session_dir: Path) -> bool:
        try:
            mtime = (session_dir / _MANIFEST).stat().st_mtime
        except OSError:
            return True
        return self._clock() - mtime > self._ttl

    def _touch(self, session_dir: Path) -> None:
        now = self._clock()
        os.utime(session_dir / _MANIFEST, (now, now))

    def last_activity(self, upload: ChunkedUpload) -> float:
        return (self._dir(upload.upload_id) / _MANIFEST).stat().st_mtime

    # ----- Session lifecycle (sync; call through ``asyncio.to_thread``) -----

    def create(
        self,
        *,
        agent_id: str,
        owner_id: int,
        filename: str,
        media_type: str,
        size: int,
        max_bytes: int,
    ) -> ChunkedUpload:
        """Start a session for a *size*-byte file (rejected above *max_bytes*)."""
        if size > max_bytes:
            raise _too_large(max_bytes)
        self.sweep_expired()
        upload = ChunkedUpload(
            upload_id=new_ulid(),
            agent_id=agent_id,
            owner_id=owner_id,
            filename=filename,
            media_type=media_type,
            size=size,
            chunk_size=self._chunk_size,
            created_at=self._clock(),
        )
        session_dir = self._root / upload.upload_id
        (session_dir / _CHUNKS).mkdir(parents=True)
        with (session_dir / _DATA).open("wb") as fh:
            fh.truncate(size)
        (session_dir / _MANIFEST).write_text(json.dumps(asdict(upload)), encoding="utf-8")
        self._touch(session_dir)
        return upload

    def get(self, upload_id: str, *, agent_id: str, owner_id: int) -> ChunkedUpload:
        """Load a live session owned by *owner_id* for *agent_id*."""
        session_dir = self._dir(upload_id)
        if self._expired(session_dir):
            shutil.rmtree(session_dir, ignore_errors=True)
            raise _not_found(upload_id)
        try:
            raw = json.loads((session_dir / _MANIFEST).read_text(encoding="utf-8"))
            upload = ChunkedUpload(**raw)
        except (OSError, ValueError, TypeError) as exc:
            raise _not_found(upload_id) from exc
        if upload.agent_id != agent_id or upload.owner_id != owner_id:
            raise _not_found(upload_id)
        return upload

    def received(self, upload: ChunkedUpload) -> list[int]:
        chunks_dir = self._dir(upload.upload_id) / _CHUNKS
        try:
            names = os.listdir(chunks_dir)
        except OSError:
            return []
        return sorted(int(n) for n in names if n.isdigit() and int(n) < upload.chunk_count)

    def _missing(self, upload: ChunkedUpload) -> list[int]:
        have = set(self.received(upload))
        return [i for i in range(upload.chunk_count) if i not in have]

    def claim(self, upload: ChunkedUpload) -> Path:
        """Take the finished file out of the session; raise if chunks are missing.

        The rename makes later chunk writes fail, and the marker check after it
        catches a re-send that started earlier (markers are removed before a
        chunk is rewritten).
        """
        session_dir = self._dir(upload.upload_id)
        missing = self._missing(upload)
        if missing:
            raise self._incomplete(missing)
        assembled = session_dir / _ASSEMBLED
        try:
            os.replace(session_dir / _DATA, assembled)
        except FileNotFoundError as exc:
            raise _not_found(upload.upload_id) from exc
        missing = self._missing(upload)
        if missing:
            self.release(upload, assembled)
            raise self._incomplete(missing)
        return assembled

    @staticmethod
    def _incomplete(missing: list[int]) -> OctopError:
        return OctopError(
            ErrorCode.UPLOAD_INCOMPLETE,
            f"{len(missing)} chunk(s) missing",
            details={"missing": len(missing), "missing_chunks": missing[:100]},
        )

    def release(self, upload: ChunkedUpload, assembled: Path) -> None:
        """Undo :meth:`claim` after a failed finish so the client can retry."""
        if assembled.exists():
            os.replace(assembled, self._dir(upload.upload_id) / _DATA)

    def discard(self, upload_id: str) -> None:
        shutil.rmtree(self._dir(upload_id), ignore_errors=True)

    def sweep_expired(self) -> int:
        """Delete sessions (and stray spool files) idle longer than the TTL."""
        try:
            entries = list(self._root.iterdir())
        except OSError:
            return 0
        removed = 0
        for entry in entries:
            if entry.is_dir():
                if self._expired(entry):
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
                continue
            try:
                stale = self._clock() - entry.stat().st_mtime > self._ttl
            except OSError:
                continue
            if stale:
                entry.unlink(missing_ok=True)
                removed += 1
        return removed

    async def spool(self, upload: AsyncByteReader, *, max_bytes: int) -> Path:
        """Copy a one-shot upload to a temp file, aborting above *max_bytes*.

        The caller owns the returned file and must remove it if it is not moved.
        """
        await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)
        target = self._root / f"spool-{new_ulid()}.bin"
        fh: BinaryIO = await asyncio.to_thread(target.open, "wb")
        total = 0
        try:
            while True:
                piece = await upload.read(_WRITE_BUFFER)
                if not piece:
                    break
                total += len(piece)
                if total > max_bytes:
                    raise _too_large(max_bytes)
                await asyncio.to_thread(fh.write, piece)
        except BaseException:
            await asyncio.to_thread(fh.close)
            await asyncio.to_thread(target.unlink, missing_ok=True)
            raise
        await asyncio.to_thread(fh.close)
        return target

    # ----- Chunk ingest -----

    async def write_chunk(
        self,
        upload: ChunkedUpload,
        index: int,
        body: AsyncIterator[bytes],
    ) -> int:
        """Stream *body* into chunk *index*; it must be exactly the expected length."""
        expected = upload.chunk_length(index)
        session_dir = self._dir(upload.upload_id)
        marker = session_dir / _CHUNKS / str(index)
        await asyncio.to_thread(marker.unlink, missing_ok=True)
        try:
            fh: BinaryIO = await asyncio.to_thread((session_dir / _DATA).open, "r+b")
        except FileNotFoundError as exc:
            raise _not_found(upload.upload_id) from exc
        written = 0
        try:
            await asyncio.to_thread(fh.seek, index * upload.chunk_size)
            pending: list[bytes] = []
            pending_len = 0
            async for piece in body:
                if not piece:
                    continue
                written += len(piece)
                if written > expected:
                    raise self._bad_length(index, expected, written)
                pending.append(piece)
                pending_len += len(piece)
                if pending_len >= _WRITE_BUFFER:
                    await asyncio.to_thread(fh.write, b"".join(pending))
                    pending, pending_len = [], 0
            if pending:
                await asyncio.to_thread(fh.write, b"".join(pending))
        finally:
            await asyncio.to_thread(fh.close)
        if written != expected:
            raise self._bad_length(index, expected, written)
        await asyncio.to_thread(marker.touch)
        await asyncio.to_thread(self._touch, session_dir)
        return written

    @staticmethod
    def _bad_length(index: int, expected: int, got: int) -> OctopError:
        return OctopError(
            ErrorCode.UPLOAD_CHUNK_INVALID,
            f"chunk {index} must be {expected} bytes, got {got}",
            details={"index": index, "expected": expected},
        )


def chunked_upload_store() -> ChunkedUploadStore:
    """Store under ``~/.octop/uploads/chunked`` (follows ``OCTOP_HOME``)."""
    return ChunkedUploadStore(PathLayout.from_env().chunked_uploads_dir, chunk_size=CHUNK_BYTES)


__all__ = [
    "CHUNK_BYTES",
    "SESSION_TTL_SECONDS",
    "ChunkedUpload",
    "ChunkedUploadStore",
    "chunked_upload_store",
]
