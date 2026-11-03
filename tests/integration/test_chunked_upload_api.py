"""Integration tests for chunked chat attachment uploads."""

from __future__ import annotations

import re
from typing import Any

import pytest

from octop.infra.gateway.media import chunked_upload
from octop.infra.utils.paths import PathLayout
from tests.support.auth import create_user

CHUNK = 8
DATA = bytes(range(256)) * 2 + b"tail"  # 516 bytes -> 65 chunks of 8


@pytest.fixture(autouse=True)
def _small_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chunked_upload, "CHUNK_BYTES", CHUNK)


def _base(aid: str) -> str:
    return f"/api/agents/{aid}/upload/chunked"


async def _start(client: Any, aid: str, auth: dict[str, str], **body: Any) -> Any:
    payload = {"filename": "第1集.mp4", "size": len(DATA), "content_type": "video/mp4", **body}
    return await client.post(_base(aid), json=payload, headers=auth)


async def _put(client: Any, aid: str, auth: dict[str, str], upload_id: str, index: int) -> Any:
    piece = DATA[index * CHUNK : (index + 1) * CHUNK]
    return await client.put(
        f"{_base(aid)}/{upload_id}/chunks/{index}",
        content=piece,
        headers={**auth, "Content-Type": "application/octet-stream"},
    )


async def test_chunked_upload_matches_single_upload(env_with_agent: Any) -> None:
    client, _srv, auth, aid = env_with_agent
    r = await _start(client, aid, auth)
    assert r.status_code == 200, r.text
    session = r.json()
    assert session["chunk_size"] == CHUNK
    assert session["chunk_count"] == 65
    assert session["received"] == []
    upload_id = session["upload_id"]

    for index in reversed(range(session["chunk_count"])):  # any order works
        r = await _put(client, aid, auth, upload_id, index)
        assert r.status_code == 200, r.text
    assert r.json() == {"index": 0, "size": CHUNK}

    r = await client.post(f"{_base(aid)}/{upload_id}/complete", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"path", "workspace_path", "url", "access_url", "filename", "media_type"}
    assert body["path"] == body["workspace_path"]
    assert re.search(r"^inbound/\d{10,}_第1集\.mp4$", body["path"])
    assert body["filename"] == "第1集.mp4"
    assert body["media_type"] == "video/mp4"

    r = await client.get(
        f"/api/agents/{aid}/workspace/download", params={"path": body["path"]}, headers=auth
    )
    assert r.status_code == 200, r.text
    assert r.content == DATA
    session_dir = PathLayout.from_env().chunked_uploads_dir / upload_id
    assert not (session_dir / "data.bin").exists()
    assert not (session_dir / "assembled.bin").exists()

    # A client that lost the response can check status and ask again.
    status = (await client.get(f"{_base(aid)}/{upload_id}", headers=auth)).json()
    assert status["received"] == list(range(65))
    r = await client.post(f"{_base(aid)}/{upload_id}/complete", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json() == body
    assert (await _put(client, aid, auth, upload_id, 0)).status_code == 404


async def test_missing_chunks_then_resume(env_with_agent: Any) -> None:
    client, _srv, auth, aid = env_with_agent
    upload_id = (await _start(client, aid, auth)).json()["upload_id"]
    for index in range(0, 65, 2):
        assert (await _put(client, aid, auth, upload_id, index)).status_code == 200

    r = await client.post(f"{_base(aid)}/{upload_id}/complete", headers=auth)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "UPLOAD_INCOMPLETE"
    assert err["details"]["missing"] == 32

    status = (await client.get(f"{_base(aid)}/{upload_id}", headers=auth)).json()
    missing = sorted(set(range(65)) - set(status["received"]))
    for index in missing:
        assert (await _put(client, aid, auth, upload_id, index)).status_code == 200
    r = await client.post(f"{_base(aid)}/{upload_id}/complete", headers=auth)
    assert r.status_code == 200, r.text


async def test_bad_chunks_rejected(env_with_agent: Any) -> None:
    client, _srv, auth, aid = env_with_agent
    upload_id = (await _start(client, aid, auth)).json()["upload_id"]
    url = f"{_base(aid)}/{upload_id}/chunks"
    for index, content in ((0, b"short"), (0, b"x" * (CHUNK + 1)), (65, b"x" * 4), (64, b"x" * 8)):
        r = await client.put(f"{url}/{index}", content=content, headers=auth)
        assert r.status_code == 400, (index, r.text)
        assert r.json()["error"]["code"] == "UPLOAD_CHUNK_INVALID"
    status = (await client.get(f"{_base(aid)}/{upload_id}", headers=auth)).json()
    assert status["received"] == []


async def test_size_over_limit_rejected_up_front(env_with_agent: Any) -> None:
    client, srv, auth, aid = env_with_agent
    r = await _start(client, aid, auth, size=srv.services.config.max_upload_bytes + 1)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "ATTACHMENT_TOO_LARGE"


async def test_unsupported_type_rejected_up_front(env_with_agent: Any) -> None:
    client, _srv, auth, aid = env_with_agent
    r = await _start(client, aid, auth, filename="weird", content_type="application/x-weird")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ATTACHMENT_UNSUPPORTED_TYPE"


async def test_cancel_removes_session(env_with_agent: Any) -> None:
    client, _srv, auth, aid = env_with_agent
    upload_id = (await _start(client, aid, auth)).json()["upload_id"]
    r = await client.delete(f"{_base(aid)}/{upload_id}", headers=auth)
    assert r.status_code == 204
    assert (await _put(client, aid, auth, upload_id, 0)).status_code == 404


async def test_other_user_cannot_use_session_or_agent(env_with_agent: Any) -> None:
    client, _srv, auth, aid = env_with_agent
    upload_id = (await _start(client, aid, auth)).json()["upload_id"]
    bob = await create_user(client, auth, username="bob")

    r = await _start(client, aid, bob)
    assert r.status_code == 403
    for r in (
        await _put(client, aid, bob, upload_id, 0),
        await client.get(f"{_base(aid)}/{upload_id}", headers=bob),
        await client.post(f"{_base(aid)}/{upload_id}/complete", headers=bob),
        await client.delete(f"{_base(aid)}/{upload_id}", headers=bob),
    ):
        assert r.status_code == 403, r.text

    r = await client.get(f"{_base(aid)}/{upload_id}", headers=auth)
    assert r.status_code == 200


async def test_requires_auth(env_with_agent: Any) -> None:
    client, _srv, _auth, aid = env_with_agent
    r = await client.post(_base(aid), json={"filename": "a.mp4", "size": 1})
    assert r.status_code == 401


async def test_single_upload_leaves_no_spool_files(env_with_agent: Any) -> None:
    client, _srv, auth, aid = env_with_agent
    files = {"file": ("note.txt", b"hello", "text/plain")}
    r = await client.post(f"/api/agents/{aid}/upload", files=files, headers=auth)
    assert r.status_code == 200, r.text
    staging = PathLayout.from_env().chunked_uploads_dir
    assert not staging.exists() or list(staging.iterdir()) == []
