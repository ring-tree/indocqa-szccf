"""附件上下文：文本切块、合成 Hit、会话问答接入与消息快照。"""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.attachments.context import build_hits, build_prompt_block
from app.core.config import get_settings
from app.graph.pipeline import _source_item
from app.main import create_app
from app.retrieval.searcher import Hit
from app.session import store as session_store


def _settings():
    return SimpleNamespace(
        attachment_text_chars_total=30000,
        attachment_text_chars_per_file=12000,
        attachment_hits_per_file=2,
        attachment_max_hits=8,
        attachment_vision=False,
    )


def _record(text: str = "保养周期为 4000 小时。空气过滤器每 2000 小时更换。") -> dict:
    return {
        "id": "att1",
        "session_id": "session001",
        "filename": "manual.md",
        "stored_name": "att1.md",
        "mime": "text/markdown",
        "kind": "document",
        "size": len(text.encode("utf-8")),
        "text": text,
        "meta": {"format": "md"},
        "parse_status": "ok",
        "created_at": "",
    }


def test_build_hits_ranks_relevant_chunk_and_marks_attachment():
    hits = build_hits([_record()], "保养周期是多少", _settings())
    assert hits
    assert hits[0].kind == "attachment"
    assert hits[0].attachment_id == "att1"
    assert hits[0].sim is None
    assert "4000" in hits[0].text


def test_build_prompt_block_includes_filename_and_text():
    block = build_prompt_block([_record()], _settings())
    assert "manual.md" in block and "4000" in block


def test_build_hits_preserves_pdf_page_marker():
    record = _record("【第3页】3号产线共有12处润滑点位。\n【第4页】保养周期为每500小时一次。")
    hits = build_hits([record], "3号产线润滑点位和保养周期", _settings())
    assert any(hit.page in {3, 4} for hit in hits)


def test_source_item_exposes_attachment_metadata():
    hit = Hit(
        chunk_id="attachment:att1:0",
        rrf=1.0,
        sim=None,
        text="保养周期为 4000 小时。",
        doc_id="attachment:att1",
        doc_name="manual.md",
        section="会话附件",
        page=None,
        kind="attachment",
        table_no=None,
        parent_text="",
        folder="",
        attachment_id="att1",
    )
    item = _source_item(hit, 1)
    assert item["kind"] == "attachment"
    assert item["attachmentId"] == "att1"
    assert item["docName"] == "manual.md"


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("INDOCQA_CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("INDOCQA_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("INDOCQA_ATTACHMENT_DIR", str(tmp_path / "attachments"))
    monkeypatch.setenv("INDOCQA_SESSION_DB", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("INDOCQA_EMBED_BACKEND", "local")
    get_settings.cache_clear()
    yield TestClient(create_app()), tmp_path
    get_settings.cache_clear()


def _upload_text(client: TestClient, data: bytes) -> tuple[str, str]:
    init = client.post(
        "/api/uploads/init",
        json={"filename": "manual.txt", "size": len(data), "mime": "text/plain"},
    ).json()["data"]
    soon = client.put(
        f"/api/uploads/{init['uploadId']}/chunks/0?sessionId={init['sessionId']}",
        content=data,
    )
    assert soon.status_code == 200
    attachment = client.post(
        f"/api/uploads/{init['uploadId']}/complete",
        json={"sessionId": init["sessionId"]},
    ).json()["data"]
    return init["sessionId"], attachment["id"]


def _parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.strip().split("\n\n"):
        event = {"event": None, "data": None}
        for line in block.splitlines():
            if line.startswith("event: "):
                event["event"] = line[7:]
            elif line.startswith("data: "):
                event["data"] = json.loads(line[6:])
        if event["event"]:
            events.append(event)
    return events


def test_attachment_only_chat_bypasses_empty_kb_and_cites_attachment(ctx):
    client, _ = ctx
    data = "设备保养周期为 4000 小时。空气过滤器每 2000 小时更换。".encode()
    session_id, attachment_id = _upload_text(client, data)
    response = client.post(
        "/api/chat",
        json={
            "message": "",
            "mode": "kb",
            "sessionId": session_id,
            "attachmentIds": [attachment_id],
        },
    )
    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert "done" in [event["event"] for event in events]
    sources = next(event for event in events if event["event"] == "sources")["data"]["items"]
    assert sources and sources[0]["kind"] == "attachment"
    assert sources[0]["attachmentId"] == attachment_id
    tokens = "".join(
        event["data"]["delta"] for event in events if event["event"] == "token"
    )
    assert "4000" in tokens


def test_follow_up_reuses_recent_session_attachment(ctx):
    client, _ = ctx
    data = "设备保养周期为 4000 小时。".encode()
    session_id, _attachment_id = _upload_text(client, data)
    client.post(
        "/api/chat",
        json={
            "message": "设备保养周期是多少？",
            "mode": "kb",
            "sessionId": session_id,
        },
    )
    response = client.post(
        "/api/chat",
        json={
            "message": "这个附件里的保养周期是多少？",
            "mode": "kb",
            "sessionId": session_id,
        },
    )
    events = _parse_sse(response.text)
    sources = next(event for event in events if event["event"] == "sources")["data"]["items"]
    assert sources and sources[0]["kind"] == "attachment"
    tokens = "".join(
        event["data"]["delta"] for event in events if event["event"] == "token"
    )
    assert "4000" in tokens


def test_message_attachments_round_trip(tmp_path):
    db = str(tmp_path / "sessions.db")
    session_store.create_session(db, "附件会话", "attsession01")
    session_store.append_message(
        db,
        "attsession01",
        "user",
        "",
        attachments=[{"id": "a1", "filename": "manual.txt", "kind": "document"}],
    )
    messages = session_store.get_messages(db, "attsession01")
    assert messages[0]["attachments"][0]["id"] == "a1"
    assert messages[0]["attachments"][0]["filename"] == "manual.txt"
