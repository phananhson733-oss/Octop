"""Unit tests for chunked chat uploads and moving host files into ``inbound/``."""

from __future__ import annotations

import io
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from deepagents.backends.local_shell import LocalShellBackend
from octop_harness.backends.workspace import BackendWorkspace

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.media.chunked_upload import ChunkedUpload, ChunkedUploadStore
from octop.infra.gateway.media.inbound_store import read_inbound_bytes, write_inbound_file

DATA = b"0123456789"  # 10 bytes -> chunks of 4: [0:4] [4:8] [8:10]


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


async def _body(data: bytes, piece: int = 3) -> AsyncIterator[bytes]:
    for start in range(0, len(data), piece):
        yield data[start : start + piece]


def _store(tmp_path: Path, clock: _Clock | None = None) -> ChunkedUploadStore:
    return ChunkedUploadStore(tmp_path / "chunked", chunk_size=4, clock=clock or _Clock())


def _create(store: ChunkedUploadStore, *, size: int = len(DATA)) -> ChunkedUpload:
    return store.create(
        agent_id="a1",
        owner_id=7,
        filename="clip.mp4",
        media_type="video/mp4",
        size=size,
        max_bytes=1024,
    )


async def _send(store: ChunkedUploadStore, upload: ChunkedUpload, index: int) -> int:
    start = index * upload.chunk_size
    return await store.write_chunk(upload, index, _body(DATA[start : start + upload.chunk_size]))


async def test_chunks_out_of_order_assemble_in_place(tmp_path: Path) -> None:
    store = _store(tmp_path)
    upload = _create(store)
    assert upload.chunk_count == 3
    assert await _send(store, upload, 2) == 2
    assert await _send(store, upload, 0) == 4
    assert store.received(upload) == [0, 2]
    await _send(store, upload, 1)
    assembled = store.claim(upload)
    assert assembled.read_bytes() == DATA


async def test_resent_chunk_overwrites(tmp_path: Path) -> None:
    store = _store(tmp_path)
    upload = _create(store)
    await store.write_chunk(upload, 0, _body(b"XXXX"))
    for index in range(3):
        await _send(store, upload, index)
    assert store.claim(upload).read_bytes() == DATA


async def test_claim_reports_missing_chunks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    upload = _create(store)
    await _send(store, upload, 0)
    with pytest.raises(OctopError) as exc:
        store.claim(upload)
    assert exc.value.code is ErrorCode.UPLOAD_INCOMPLETE
    assert exc.value.details["missing_chunks"] == [1, 2]


async def test_claim_detects_chunk_rewritten_during_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    upload = _create(store)
    for index in range(3):
        await _send(store, upload, index)
    session = tmp_path / "chunked" / upload.upload_id
    real_replace = os.replace

    def replace_while_resending(src: Path, dst: Path) -> None:
        # A re-send of chunk 1 starts (drops its marker) right before the rename.
        (session / "chunks" / "1").unlink(missing_ok=True)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace_while_resending)
    with pytest.raises(OctopError) as exc:
        store.claim(upload)
    assert exc.value.code is ErrorCode.UPLOAD_INCOMPLETE
    assert (session / "data.bin").exists(), "file goes back so the client can retry"


@pytest.mark.parametrize("payload", [b"abc", b"abcde"])
async def test_wrong_chunk_length_rejected(tmp_path: Path, payload: bytes) -> None:
    store = _store(tmp_path)
    upload = _create(store)
    with pytest.raises(OctopError) as exc:
        await store.write_chunk(upload, 0, _body(payload))
    assert exc.value.code is ErrorCode.UPLOAD_CHUNK_INVALID
    assert store.received(upload) == []


async def test_index_out_of_range(tmp_path: Path) -> None:
    store = _store(tmp_path)
    upload = _create(store)
    for index in (-1, 3):
        with pytest.raises(OctopError) as exc:
            await store.write_chunk(upload, index, _body(b"0123"))
        assert exc.value.code is ErrorCode.UPLOAD_CHUNK_INVALID


def test_create_rejects_size_over_limit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(OctopError) as exc:
        _create(store, size=2048)
    assert exc.value.code is ErrorCode.ATTACHMENT_TOO_LARGE
    assert not (tmp_path / "chunked").exists() or not any((tmp_path / "chunked").iterdir())


def test_get_checks_owner_agent_and_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    upload = _create(store)
    assert store.get(upload.upload_id, agent_id="a1", owner_id=7) == upload
    for kwargs in ({"agent_id": "a2", "owner_id": 7}, {"agent_id": "a1", "owner_id": 8}):
        with pytest.raises(OctopError) as exc:
            store.get(upload.upload_id, **kwargs)  # type: ignore[arg-type]
        assert exc.value.code is ErrorCode.UPLOAD_SESSION_NOT_FOUND
    with pytest.raises(OctopError) as exc:
        store.get("../../etc", agent_id="a1", owner_id=7)
    assert exc.value.code is ErrorCode.UPLOAD_SESSION_NOT_FOUND


async def test_idle_sessions_expire_and_get_swept(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path, clock)
    idle = _create(store)
    active = _create(store)
    stray = tmp_path / "chunked" / "spool-crashed.bin"
    stray.write_bytes(b"x")
    os.utime(stray, (clock.now, clock.now))

    clock.now += store.ttl_seconds - 10
    await _send(store, active, 0)  # activity extends the session
    clock.now += 20
    with pytest.raises(OctopError) as exc:
        store.get(idle.upload_id, agent_id="a1", owner_id=7)
    assert exc.value.code is ErrorCode.UPLOAD_SESSION_NOT_FOUND
    assert not (tmp_path / "chunked" / idle.upload_id).exists()

    assert store.sweep_expired() == 1  # the stray spool file
    assert not stray.exists()
    assert store.get(active.upload_id, agent_id="a1", owner_id=7) == active


async def test_create_sweeps_expired_sessions(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path, clock)
    old = _create(store)
    clock.now += store.ttl_seconds + 1
    _create(store)
    assert not (tmp_path / "chunked" / old.upload_id).exists()


async def test_spool_caps_size_and_cleans_up(tmp_path: Path) -> None:
    store = _store(tmp_path)

    class _Reader:
        def __init__(self, data: bytes) -> None:
            self._buf = io.BytesIO(data)

        async def read(self, size: int = -1) -> bytes:
            return self._buf.read(size)

    spooled = await store.spool(_Reader(DATA), max_bytes=10)
    assert spooled.read_bytes() == DATA
    with pytest.raises(OctopError) as exc:
        await store.spool(_Reader(DATA + b"!"), max_bytes=10)
    assert exc.value.code is ErrorCode.ATTACHMENT_TOO_LARGE
    assert [p.name for p in (tmp_path / "chunked").iterdir()] == [spooled.name]


async def test_write_inbound_file_moves_source(tmp_path: Path) -> None:
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    workspace = BackendWorkspace(
        LocalShellBackend(root_dir=str(ws_dir), virtual_mode=False), ws_dir
    )
    source = tmp_path / "upload.bin"
    source.write_bytes(DATA)
    stored = await write_inbound_file(
        workspace, source, filename="剧集 1.mp4", media_type="video/mp4"
    )
    assert stored.path.startswith("inbound/") and stored.path.endswith("_剧集 1.mp4")
    assert stored.filename == "剧集 1.mp4"
    assert stored.media_type == "video/mp4"
    assert stored.size == len(DATA)
    assert not source.exists()
    assert await read_inbound_bytes(workspace, stored.path) == DATA


async def test_write_inbound_file_enforces_limit(tmp_path: Path) -> None:
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    workspace = BackendWorkspace(
        LocalShellBackend(root_dir=str(ws_dir), virtual_mode=False), ws_dir
    )
    source = tmp_path / "upload.bin"
    source.write_bytes(DATA)
    with pytest.raises(OctopError) as exc:
        await write_inbound_file(
            workspace, source, filename="a.mp4", media_type="video/mp4", max_bytes=4
        )
    assert exc.value.code is ErrorCode.ATTACHMENT_TOO_LARGE
    assert source.exists()
