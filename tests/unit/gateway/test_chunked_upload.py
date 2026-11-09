"""Unit tests for chunked chat uploads and moving host files into ``inbound/``."""

from __future__ import annotations

import asyncio
import io
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from deepagents.backends.local_shell import LocalShellBackend
from octop_harness.backends.workspace import BackendWorkspace

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.media import chunked_upload, inbound_store
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
    assembled = await store.claim(upload)
    assert assembled.read_bytes() == DATA


async def test_resent_chunk_overwrites(tmp_path: Path) -> None:
    store = _store(tmp_path)
    upload = _create(store)
    await store.write_chunk(upload, 0, _body(b"XXXX"))
    for index in range(3):
        await _send(store, upload, index)
    assert (await store.claim(upload)).read_bytes() == DATA


async def test_claim_reports_missing_chunks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    upload = _create(store)
    await _send(store, upload, 0)
    with pytest.raises(OctopError) as exc:
        await store.claim(upload)
    assert exc.value.code is ErrorCode.UPLOAD_INCOMPLETE
    assert exc.value.details["missing_chunks"] == [1, 2]


async def test_overlapping_resends_of_one_chunk_never_mix(tmp_path: Path) -> None:
    store = _store(tmp_path)
    upload = _create(store)
    for index in (0, 2):
        await _send(store, upload, index)

    async def slow(data: bytes) -> AsyncIterator[bytes]:
        for byte in data:
            await asyncio.sleep(0)
            yield bytes([byte])

    await asyncio.gather(
        store.write_chunk(upload, 1, slow(b"AAAA")),
        store.write_chunk(upload, 1, slow(b"BBBB")),
    )
    middle = (await store.claim(upload)).read_bytes()[4:8]
    assert middle in (b"AAAA", b"BBBB")
    assert not list((tmp_path / "chunked" / upload.upload_id / "parts").iterdir())


async def test_failed_resend_keeps_stored_chunk(tmp_path: Path) -> None:
    store = _store(tmp_path)
    upload = _create(store)
    for index in range(3):
        await _send(store, upload, index)
    with pytest.raises(OctopError):
        await store.write_chunk(upload, 1, _body(b"zz"))
    assert store.received(upload) == [0, 1, 2]
    assert (await store.claim(upload)).read_bytes() == DATA


async def test_chunk_after_claim_is_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    upload = _create(store)
    for index in range(3):
        await _send(store, upload, index)
    assembled = await store.claim(upload)
    with pytest.raises(OctopError) as exc:
        await store.write_chunk(upload, 0, _body(b"XXXX"))
    assert exc.value.code is ErrorCode.UPLOAD_SESSION_NOT_FOUND
    assert assembled.read_bytes() == DATA


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


def test_sweep_keeps_session_still_being_created(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path, clock)
    creating = tmp_path / "chunked" / "01M6DYTKY2S5Q284DWJNJC6F5Z"
    creating.mkdir(parents=True)  # no manifest yet
    os.utime(creating, (clock.now, clock.now))
    assert store.sweep_expired() == 0
    assert creating.exists()
    clock.now += store.ttl_seconds + 1
    assert store.sweep_expired() == 1
    assert not creating.exists()


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


async def test_write_inbound_file_removes_placeholder_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    workspace = BackendWorkspace(
        LocalShellBackend(root_dir=str(ws_dir), virtual_mode=False), ws_dir
    )
    source = tmp_path / "upload.bin"
    source.write_bytes(DATA)

    def boom(*_args: object, **_kwargs: object) -> bool:
        raise OSError("disk full")

    monkeypatch.setattr(inbound_store, "_place_local_file", boom)
    with pytest.raises(OSError):
        await write_inbound_file(workspace, source, filename="a.mp4", media_type="video/mp4")
    assert list((ws_dir / "inbound").iterdir()) == []
    assert source.exists()


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


async def test_finish_keeps_receipt_until_ttl(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path, clock)
    upload = _create(store)
    for index in range(upload.chunk_count):
        await _send(store, upload, index)
    await store.claim(upload)
    store.finish(upload, {"path": "inbound/x.mp4"})
    assert store.receipt(upload) == {"path": "inbound/x.mp4"}
    assert sorted(p.name for p in (tmp_path / "chunked" / upload.upload_id).iterdir()) == [
        "chunks",
        "manifest.json",
        "receipt.json",
    ]
    clock.now += store.ttl_seconds + 1
    assert store.sweep_expired() == 1
    assert not (tmp_path / "chunked" / upload.upload_id).exists()


async def test_background_sweep_runs_at_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    class _Fake:
        def sweep_expired(self) -> int:
            calls.append(1)
            return 0

    monkeypatch.setattr(chunked_upload, "chunked_upload_store", _Fake)
    task = asyncio.create_task(chunked_upload.sweep_chunked_uploads_forever(3600))
    for _ in range(50):
        if calls:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == [1]


async def test_claim_recovers_assembled_file_after_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    upload = _create(store)
    for index in range(upload.chunk_count):
        await _send(store, upload, index)
    await store.claim(upload)
    restarted = _store(tmp_path)
    assert (await restarted.claim(upload)).read_bytes() == DATA


async def test_concurrent_same_name_uploads_keep_both_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = BackendWorkspace(
        LocalShellBackend(root_dir=str(tmp_path), virtual_mode=False), tmp_path
    )
    monkeypatch.setattr(inbound_store.time, "time", lambda: 1_800_000_000)
    first, second = tmp_path / "one.bin", tmp_path / "two.bin"
    first.write_bytes(b"AAAA")
    second.write_bytes(b"BBBB")
    results = await asyncio.gather(
        write_inbound_file(workspace, first, filename="episode.mp4", media_type="video/mp4"),
        write_inbound_file(workspace, second, filename="episode.mp4", media_type="video/mp4"),
    )
    assert len({r.path for r in results}) == 2
    assert {await read_inbound_bytes(workspace, r.path) for r in results} == {b"AAAA", b"BBBB"}


@pytest.mark.parametrize("filename", ["episode", "影" * 80])
async def test_recoverable_upload_keeps_extension_and_original_name(
    tmp_path: Path, filename: str
) -> None:
    from octop.infra.gateway.media.inbound_retention import _is_expired_media

    workspace = BackendWorkspace(
        LocalShellBackend(root_dir=str(tmp_path), virtual_mode=False), tmp_path
    )
    source = tmp_path / "assembled.bin"
    source.write_bytes(DATA)
    name = inbound_store.build_timestamped_inbound_name(
        filename, now=1_700_000_000, upload_id="01ARZ3NDEKTSV4RRFFQ69G5FAV"
    )
    stored = await write_inbound_file(
        workspace,
        source,
        filename=filename,
        media_type="video/mp4",
        stored_name=name,
        preserve_source=True,
    )
    assert stored.filename == filename + ".mp4"
    assert stored.path.endswith(".mp4")
    assert len(Path(stored.path).name.encode("utf-8")) <= 255
    assert inbound_store.display_name_from_stored(Path(stored.path).name).endswith(".mp4")
    assert _is_expired_media(Path(stored.path).name, cutoff=1_800_000_000)
    assert source.read_bytes() == DATA


async def test_old_unrecoverable_session_is_gone_so_browser_starts_again(tmp_path: Path) -> None:
    store = _store(tmp_path)
    upload = _create(store)
    for index in range(upload.chunk_count):
        await _send(store, upload, index)
    (await store.claim(upload)).unlink()  # old server moved it before crashing without a receipt
    with pytest.raises(OctopError) as exc:
        store.get(upload.upload_id, agent_id=upload.agent_id, owner_id=upload.owner_id)
    assert exc.value.code == ErrorCode.UPLOAD_SESSION_NOT_FOUND


async def test_long_unicode_suffix_never_truncates_upload_identity(tmp_path: Path) -> None:
    workspace = BackendWorkspace(
        LocalShellBackend(root_dir=str(tmp_path), virtual_mode=False), tmp_path
    )
    results = []
    for identity, payload in [
        ("01ARZ3NDEKTSV4RRFFQ69G5FAV", b"A"),
        ("01ARZ3NDEKTSV4RRFFQ69G5FAW", b"B"),
    ]:
        name = "a." + "影" * 80
        source = tmp_path / identity
        source.write_bytes(payload)
        stored_name = inbound_store.build_timestamped_inbound_name(
            name, now=1_800_000_000, upload_id=identity
        )
        result = await write_inbound_file(
            workspace,
            source,
            filename=name,
            media_type="video/mp4",
            stored_name=stored_name,
            preserve_source=True,
        )
        assert identity in result.path
        assert len(Path(result.path).name.encode("utf-8")) <= 255
        results.append(result)
    assert results[0].path != results[1].path
    assert await read_inbound_bytes(workspace, results[0].path) == b"A"
    assert await read_inbound_bytes(workspace, results[1].path) == b"B"
