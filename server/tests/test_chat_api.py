"""API 层阶段测试（T1.5 / T1.6）：SSE 问答、引用、错误码、入库-删除闭环。"""

import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.indexing import store
from app.indexing.ingest import ingest_file
from app.main import create_app

CORPUS = pathlib.Path(__file__).resolve().parents[2] / "data" / "corpus"
FIXTURE = CORPUS / "fixture_术语混写_模拟维保手册.md"


def _parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.strip().split("\n\n"):
        ev = {"event": None, "data": None}
        for line in block.splitlines():
            if line.startswith("event: "):
                ev["event"] = line[7:]
            elif line.startswith("data: "):
                ev["data"] = json.loads(line[6:])
        if ev["event"]:
            events.append(ev)
    return events


def _tokens(events: list[dict]) -> str:
    return "".join(e["data"]["delta"] for e in events if e["event"] == "token")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import shutil

    seed = pathlib.Path(__file__).resolve().parents[2] / "data" / "terms" / "seed.json"
    terms_dir = tmp_path / "terms"
    terms_dir.mkdir(exist_ok=True)
    shutil.copy(seed, terms_dir / "seed.json")
    monkeypatch.setenv("INDOCQA_CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("INDOCQA_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("INDOCQA_SESSION_DB", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("INDOCQA_EMBED_BACKEND", "local")
    monkeypatch.setenv("INDOCQA_TERMS_FILE", str(terms_dir / "terms.json"))
    get_settings.cache_clear()
    store._client.cache_clear()
    ingest_file(FIXTURE)
    yield TestClient(create_app())
    get_settings.cache_clear()
    store._client.cache_clear()


def test_kb_chat_stream_with_sources_and_done(client):
    r = client.post(
        "/api/chat",
        json={"message": "MTBF 和平均无故障时间是同一个指标吗？额定值是多少？", "mode": "kb"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(r.text)
    kinds = [e["event"] for e in events]
    assert "sources" in kinds and "done" in kinds and "trace" in kinds
    trace = next(e for e in events if e["event"] == "trace")["data"]
    assert trace["stage"] == "classify" and trace["intent"]
    items = next(e for e in events if e["event"] == "sources")["data"]["items"]
    assert items and items[0]["docName"].endswith(".md")
    assert "MTBF" in _tokens(events) or "8000" in _tokens(events)
    done = next(e for e in events if e["event"] == "done")["data"]
    assert done["confidence"] in {"high", "mid", "low"}
    assert done.get("request_id")


def test_kb_table_question_hits_serialized_table(client):
    r = client.post("/api/chat", json={"message": "LUB 更换的保养周期是多少小时？", "mode": "kb"})
    events = _parse_sse(r.text)
    assert "4000" in _tokens(events)


def test_direct_without_gateway_returns_42901(client, monkeypatch):
    # 环境变量覆盖 .env 中的网关配置（含备用供应商），模拟完全未配置场景
    for var in (
        "INDOCQA_LLM_BASE_URL",
        "INDOCQA_LLM_API_KEY",
        "INDOCQA_LLM_MODEL",
        "INDOCQA_LLM_BACKUP_BASE_URL",
        "INDOCQA_LLM_BACKUP_API_KEY",
        "INDOCQA_LLM_BACKUP_MODEL",
    ):
        monkeypatch.setenv(var, "")
    get_settings.cache_clear()
    try:
        r = client.post("/api/chat", json={"message": "你好", "mode": "direct"})
        assert r.status_code == 502
        assert r.json()["error"]["code"] == 42901
    finally:
        get_settings.cache_clear()


def test_kb_empty_returns_40401(tmp_path, monkeypatch):
    monkeypatch.setenv("INDOCQA_CHROMA_DIR", str(tmp_path / "fresh_chroma"))
    monkeypatch.setenv("INDOCQA_UPLOAD_DIR", str(tmp_path / "fresh_uploads"))
    monkeypatch.setenv("INDOCQA_SESSION_DB", str(tmp_path / "fresh_sessions.db"))
    monkeypatch.setenv("INDOCQA_EMBED_BACKEND", "local")
    get_settings.cache_clear()
    store._client.cache_clear()
    try:
        c = TestClient(create_app())
        r = c.post("/api/chat", json={"message": "任意问题", "mode": "kb"})
        assert r.status_code == 404
        assert r.json()["error"]["code"] == 40401
    finally:
        get_settings.cache_clear()
        store._client.cache_clear()


def test_ingest_documents_delete_cycle(client):
    with open(FIXTURE, "rb") as f:
        r = client.post("/api/kb/ingest", files={"file": (FIXTURE.name, f, "text/markdown")})
    assert r.status_code == 200
    assert r.json()["data"]["duplicated"] is True  # 内容哈希幂等
    docs = client.get("/api/kb/documents").json()["data"]
    assert any(d["filename"] == FIXTURE.name for d in docs)
    doc_id = docs[0]["docId"]
    assert client.delete(f"/api/kb/documents/{doc_id}").json()["data"] is True
    assert client.delete(f"/api/kb/documents/{doc_id}").status_code == 404


def test_ingest_rejects_unsupported_format(client):
    r = client.post(
        "/api/kb/ingest", files={"file": ("tool.exe", b"MZ9000000", "application/octet-stream")}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == 40003
