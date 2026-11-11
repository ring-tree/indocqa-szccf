"""会话附件分片上传、内容读取、权限与清理测试。"""

import io
import pathlib

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.core.config import get_settings
from app.main import create_app


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("INDOCQA_CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("INDOCQA_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("INDOCQA_ATTACHMENT_DIR", str(tmp_path / "attachments"))
    monkeypatch.setenv("INDOCQA_SESSION_DB", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("INDOCQA_EMBED_BACKEND", "local")
    get_settings.cache_clear()
    yield TestClient(create_app()), get_settings(), tmp_path
    get_settings.cache_clear()


def _init(client: TestClient, filename: str, data: bytes, session_id: str | None = None) -> dict:
    body = {"filename": filename, "size": len(data), "mime": "application/octet-stream"}
    if session_id:
        body["sessionId"] = session_id
    response = client.post("/api/uploads/init", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _put_chunks(client: TestClient, init: dict, data: bytes) -> None:
    chunk_size = init["chunkSize"]
    for index in range(init["totalChunks"]):
        chunk = data[index * chunk_size : (index + 1) * chunk_size]
        response = client.put(
            f"/api/uploads/{init['uploadId']}/chunks/{index}?sessionId={init['sessionId']}",
            content=chunk,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert response.status_code == 200, response.text


def _complete(client: TestClient, init: dict) -> dict:
    response = client.post(
        f"/api/uploads/{init['uploadId']}/complete",
        json={"sessionId": init["sessionId"]},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _upload(client: TestClient, filename: str, data: bytes, session_id: str | None = None) -> dict:
    init = _init(client, filename, data, session_id)
    _put_chunks(client, init, data)
    return _complete(client, init)


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 6), (15, 118, 110)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_upload_text_creates_session_and_content(ctx):
    client, settings, _ = ctx
    data = "设备保养周期为 4000 小时。".encode()
    init = _init(client, "note.txt", data)
    assert init["sessionId"]
    assert client.get(f"/api/sessions/{init['sessionId']}").status_code == 200
    _put_chunks(client, init, data)
    attachment = _complete(client, init)
    assert attachment["kind"] == "document"
    assert attachment["parseStatus"] == "ok"
    assert attachment["textChars"] > 0
    assert attachment["filename"] == "note.txt"

    content = client.get(
        f"/api/attachments/{attachment['id']}/content?sessionId={init['sessionId']}"
    )
    assert content.status_code == 200
    assert content.content == data
    listed = client.get(f"/api/sessions/{init['sessionId']}/attachments").json()["data"]
    assert [item["id"] for item in listed] == [attachment["id"]]
    path = pathlib.Path(settings.attachment_dir) / init["sessionId"]
    assert any(path.iterdir())


def test_upload_image_metadata_and_content_type(ctx):
    client, _, _ = ctx
    data = _png_bytes()
    attachment = _upload(client, "photo.png", data)
    assert attachment["kind"] == "image"
    assert attachment["meta"]["width"] == 8
    assert attachment["meta"]["height"] == 6
    content = client.get(
        f"/api/attachments/{attachment['id']}/content?sessionId={attachment['sessionId']}"
    )
    assert content.status_code == 200
    assert content.headers["content-type"].startswith("image/png")
    assert content.headers["x-content-type-options"] == "nosniff"


def test_chunk_retry_is_idempotent_and_status_resumes(ctx):
    client, _, _ = ctx
    data = b"abcdefghij"
    init = _init(client, "retry.txt", data)
    chunk = data[: init["chunkSize"]]
    for _ in range(2):
        response = client.put(
            f"/api/uploads/{init['uploadId']}/chunks/0?sessionId={init['sessionId']}",
            content=chunk,
        )
        assert response.status_code == 200
    status = client.get(
        f"/api/uploads/{init['uploadId']}?sessionId={init['sessionId']}"
    ).json()["data"]
    assert status["received"] == [0]
    for index in range(1, init["totalChunks"]):
        response = client.put(
            f"/api/uploads/{init['uploadId']}/chunks/{index}?sessionId={init['sessionId']}",
            content=data[index * init["chunkSize"] : (index + 1) * init["chunkSize"]],
        )
        assert response.status_code == 200
    assert _complete(client, init)["size"] == len(data)


def test_multi_chunk_upload(ctx, monkeypatch):
    client, _, _ = ctx
    monkeypatch.setenv("INDOCQA_ATTACHMENT_CHUNK_BYTES", "262144")
    get_settings.cache_clear()
    try:
        data = b"a" * 700000
        init = _init(client, "multi.txt", data)
        assert init["totalChunks"] == 3
        _put_chunks(client, init, data)
        attachment = _complete(client, init)
        assert attachment["size"] == len(data)
        assert attachment["parseStatus"] == "ok"
    finally:
        get_settings.cache_clear()


def test_complete_rejects_missing_chunk(ctx):
    client, _, _ = ctx
    data = b"hello world"
    init = _init(client, "missing.txt", data)
    # 不上传任何分片，直接完成
    response = client.post(
        f"/api/uploads/{init['uploadId']}/complete",
        json={"sessionId": init["sessionId"]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == 40006


def test_rejects_unsupported_format_and_oversize(ctx):
    client, _, _ = ctx
    r = client.post(
        "/api/uploads/init",
        json={"filename": "tool.exe", "size": 10, "mime": "application/octet-stream"},
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == 40003

    r = client.post(
        "/api/uploads/init",
        json={"filename": "big.txt", "size": 101 * 1024 * 1024, "mime": "text/plain"},
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == 40002


def test_cross_session_access_is_forbidden(ctx):
    client, _, _ = ctx
    data = b"secret"
    attachment = _upload(client, "secret.txt", data)
    other = client.post("/api/sessions", json={"title": "other"}).json()["data"]["id"]
    r = client.get(f"/api/attachments/{attachment['id']}/content?sessionId={other}")
    assert r.status_code == 403 and r.json()["error"]["code"] == 40301
    r = client.delete(f"/api/attachments/{attachment['id']}?sessionId={other}")
    assert r.status_code == 403 and r.json()["error"]["code"] == 40301


def test_abort_upload_removes_staging(ctx):
    client, settings, _ = ctx
    data = b"abort me"
    init = _init(client, "abort.txt", data)
    client.put(
        f"/api/uploads/{init['uploadId']}/chunks/0?sessionId={init['sessionId']}",
        content=data,
    )
    r = client.delete(
        f"/api/uploads/{init['uploadId']}?sessionId={init['sessionId']}"
    )
    assert r.status_code == 200
    status = client.get(
        f"/api/uploads/{init['uploadId']}?sessionId={init['sessionId']}"
    )
    assert status.status_code == 400 and status.json()["error"]["code"] == 40005
    staging = pathlib.Path(settings.attachment_dir) / ".staging" / init["uploadId"]
    assert not staging.exists()


def test_delete_session_removes_attachment_files(ctx):
    client, settings, _ = ctx
    attachment = _upload(client, "cleanup.txt", b"cleanup me")
    session_dir = pathlib.Path(settings.attachment_dir) / attachment["sessionId"]
    assert session_dir.exists()
    r = client.delete(f"/api/sessions/{attachment['sessionId']}")
    assert r.status_code == 200
    assert not session_dir.exists()


def test_size_mismatch_is_rejected(ctx):
    client, _, _ = ctx
    data = b"short"
    init = _init(client, "mismatch.txt", data + b"x")
    _put_chunks(client, init, data)
    r = client.post(
        f"/api/uploads/{init['uploadId']}/complete",
        json={"sessionId": init["sessionId"]},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == 40006
