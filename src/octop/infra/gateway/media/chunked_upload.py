"""Chunked chat uploads — large attachments sent as several small requests.

Each session lives in ``{root}/{upload_id}/``:

* ``manifest.json`` — owner, agent, filename, size; its mtime is the last activity.
* ``data.bin`` — sparse file of the final size; every chunk is written at its
  offset, so chunks may arrive in any order and be re-sent.
* ``chunks/{index}`` — empty marker written after a chunk was fully stored.
* ``parts/`` — one temp file per in-flight chunk request.
* ``receipt.json`` — the attachment payload, written once the file is stored;
  the session then keeps only its manifest, chunk markers and receipt until the
  TTL, so a client that lost the completion response can ask again.

A chunk request streams into its own part file; only once it has the exact
length is it copied into ``data.bin`` under a per-session lock, which
:meth:`ChunkedUploadStore.claim` also takes. Overlapping re-sends of one chunk
therefore never mix bytes, and a failed re-send leaves the stored chunk alone.
The lock is in-process (Octop serves from one process).

Bytes stream from the request straight to disk; nothing holds a whole chunk or
file in memory. Sessions idle longer than the TTL are removed by
:meth:`ChunkedUploadStore.sweep_expired`, which the server runs at startup and
then hourly (:func:`sweep_chunked_uploads_forever`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
import weakref
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.paths import PathLayout
from octop.infra.utils.ulid import new_ulid

logger = logging.getLogger(__name__)

CHUNK_BYTES = 32 * 1024 * 1024
SESSION_TTL_SECONDS = 24 * 60 * 60

_WRITE_BUFFER = 1024 * 1024
_UPLOAD_ID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_MANIFEST = "manifest.json"
_DATA = "data.bin"
_ASSEMBLED = "assembled.bin"
_CHUNKS = "chunks"
_PARTS = "parts"
_RECEIPT = "receipt.json"
SWEEP_INTERVAL_SECONDS = 60 * 60

_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def _session_lock(upload_id: str) -> asyncio.Lock:
    lock = _LOCKS.get(upload_id)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[upload_id] = lock
    return lock


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
        # A session being created has no manifest yet; age it by the directory.
        for candidate in (session_dir / _MANIFEST, session_dir):
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                continue
            return self._clock() - mtime > self._ttl
        return True

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
        (session_dir / _PARTS).mkdir()
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

    async def claim(self, upload: ChunkedUpload) -> Path:
        """Take the finished file out of the session; raise if chunks are missing.

        Runs under the session lock, so no chunk is half-copied into the file;
        after the rename, later chunk commits fail as not found.
        """
        async with _session_lock(upload.upload_id):
            return await asyncio.to_thread(self._claim, upload)

    def _claim(self, upload: ChunkedUpload) -> Path:
        session_dir = self._dir(upload.upload_id)
        missing = self._missing(upload)
        if missing:
            raise self._incomplete(missing)
        assembled = session_dir / _ASSEMBLED
        try:
            os.replace(session_dir / _DATA, assembled)
        except FileNotFoundError as exc:
            raise _not_found(upload.upload_id) from exc
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

    def receipt(self, upload: ChunkedUpload) -> dict[str, str] | None:
        """Payload of an already finished upload, or ``None``."""
        try:
            raw = (self._dir(upload.upload_id) / _RECEIPT).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        payload: dict[str, str] = json.loads(raw)
        return payload

    def finish(self, upload: ChunkedUpload, payload: dict[str, str]) -> None:
        """Record *payload* and drop the file data; markers stay until the TTL."""
        session_dir = self._dir(upload.upload_id)
        tmp = session_dir / f"{_RECEIPT}.tmp"
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, session_dir / _RECEIPT)
        for name in (_DATA, _ASSEMBLED):
            (session_dir / name).unlink(missing_ok=True)
        shutil.rmtree(session_dir / _PARTS, ignore_errors=True)
        self._touch(session_dir)

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
        part = session_dir / _PARTS / f"{index}.{new_ulid()}"
        try:
            fh: BinaryIO = await asyncio.to_thread(part.open, "wb")
        except FileNotFoundError as exc:
            raise _not_found(upload.upload_id) from exc
        written = 0
        try:
            try:
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
        except BaseException:
            await asyncio.to_thread(part.unlink, missing_ok=True)
            raise
        # Shielded so a dropped request cannot release the lock mid-copy.
        await asyncio.shield(self._commit_part(upload, index, part))
        return written

    async def _commit_part(self, upload: ChunkedUpload, index: int, part: Path) -> None:
        try:
            async with _session_lock(upload.upload_id):
                await asyncio.to_thread(self._commit, upload, index, part)
        finally:
            await asyncio.to_thread(part.unlink, missing_ok=True)

    def _commit(self, upload: ChunkedUpload, index: int, part: Path) -> None:
        """Copy a verified part into ``data.bin`` and mark the chunk received."""
        session_dir = self._dir(upload.upload_id)
        try:
            dst = (session_dir / _DATA).open("r+b")
        except FileNotFoundError as exc:
            raise _not_found(upload.upload_id) from exc
        with dst, part.open("rb") as src:
            dst.seek(index * upload.chunk_size)
            shutil.copyfileobj(src, dst, _WRITE_BUFFER)
        (session_dir / _CHUNKS / str(index)).touch()
        self._touch(session_dir)

    @staticmethod
    def _bad_length(index: int, expected: int, got: int) -> OctopError:
        return OctopError(
            ErrorCode.UPLOAD_CHUNK_INVALID,
            f"chunk {index} must be {expected} bytes, got {got}",
            details={"index": index, "expected": expected},
        )


async def sweep_chunked_uploads_forever(
    interval_seconds: float = SWEEP_INTERVAL_SECONDS,
) -> None:
    """Sweep expired sessions now and then every *interval_seconds* until cancelled."""
    while True:
        try:
            await asyncio.to_thread(chunked_upload_store().sweep_expired)
        except Exception:
            logger.warning("chunked upload sweep failed", exc_info=True)
        await asyncio.sleep(interval_seconds)


def chunked_upload_store() -> ChunkedUploadStore:
    """Store under ``~/.octop/uploads/chunked`` (follows ``OCTOP_HOME``)."""
    return ChunkedUploadStore(PathLayout.from_env().chunked_uploads_dir, chunk_size=CHUNK_BYTES)


__all__ = [
    "CHUNK_BYTES",
    "SESSION_TTL_SECONDS",
    "SWEEP_INTERVAL_SECONDS",
    "ChunkedUpload",
    "ChunkedUploadStore",
    "chunked_upload_store",
    "sweep_chunked_uploads_forever",
]
