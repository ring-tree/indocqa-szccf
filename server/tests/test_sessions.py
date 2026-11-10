"""会话：SQLite 存储、上下文装配、API 隔离与删除（工业语料）。"""

import json
import pathlib
import shutil

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.graph.pipeline import _context_query
from app.indexing import store
from app.indexing.ingest import ingest_file
from app.main import create_app
from app.session import store as session_store

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


@pytest.fixture()
def client(tmp_path, monkeypatch):
    seed = pathlib.Path(__file__).resolve().parents[2] / "data" / "terms" / "seed.json"
    terms_dir = tmp_path / "terms"
    terms_dir.mkdir()
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


def test_context_query_prepends_previous_question():
    history = [{"role": "user", "content": "LUB 更换的保养周期是多少小时？"}]
    assert _context_query("它的周期呢？", history).startswith("LUB")
    assert _context_query("空压机保养周期表包含哪些项目", history) == (
        "空压机保养周期表包含哪些项目"
    )


def test_session_crud_isolation(client):
    a = client.post("/api/sessions", json={"title": "会话 A"}).json()["data"]
    b = client.post("/api/sessions", json={"title": "会话 B"}).json()["data"]
    assert a["id"] != b["id"]

    listed = client.get("/api/sessions").json()["data"]
    assert {a["id"], b["id"]} <= {s["id"] for s in listed}

    client.patch(f"/api/sessions/{a['id']}", json={"title": "改名后"})
    assert client.get(f"/api/sessions/{a['id']}").json()["data"]["title"] == "改名后"

    assert client.delete(f"/api/sessions/{b['id']}").json()["data"] is True
    r = client.get(f"/api/sessions/{b['id']}")
    assert r.status_code == 404 and r.json()["error"]["code"] == 40403


def test_chat_persists_and_isolates_sessions(client):
    sid1 = "szccfsession001"
    r1 = client.post(
        "/api/chat",
        json={"message": "LUB 更换的保养周期是多少小时？", "mode": "kb", "sessionId": sid1},
    )
    done1 = next(e for e in _parse_sse(r1.text) if e["event"] == "done")["data"]
    assert done1["sessionId"] == sid1 and done1["messageId"]

    client.post(
        "/api/chat",
        json={"message": "那空气过滤器滤芯呢？", "mode": "kb", "sessionId": sid1},
    )
    s1 = client.get(f"/api/sessions/{sid1}").json()["data"]
    assert len(s1["messages"]) == 4
    assert [m["role"] for m in s1["messages"]] == ["user", "assistant", "user", "assistant"]
    assert "model" in s1["messages"][-1]

    sid2 = "szccfsession002"
    client.post(
        "/api/chat",
        json={"message": "保养周期表包含哪些项目？", "mode": "kb", "sessionId": sid2},
    )
    s2 = client.get(f"/api/sessions/{sid2}").json()["data"]
    assert len(s2["messages"]) == 2
    assert s2["messages"][0]["content"] == "保养周期表包含哪些项目？"
    assert {m["id"] for m in s1["messages"]}.isdisjoint({m["id"] for m in s2["messages"]})


def test_chat_rejects_invalid_session_id(client):
    r = client.post("/api/chat", json={"message": "你好", "mode": "kb", "sessionId": "bad id!"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == 40001


def test_cleanup_removes_sessions_older_than_ttl(tmp_path):
    import sqlite3
    import time

    db = str(tmp_path / "sessions.db")
    session_store.create_session(db, "旧会话", "oldsession001")
    old = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 8 * 86400))
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE sessions SET created_at=?, updated_at=? WHERE id=?",
        (old, old, "oldsession001"),
    )
    con.commit()
    con.close()
    assert session_store.cleanup(db, 7) == 1
    assert session_store.get_session(db, "oldsession001") is None


def test_cleanup_keeps_recently_active_session(tmp_path):
    """TTL 以最后活跃时间为准：创建早但近期有活动的会话不应被清理。"""
    import sqlite3
    import time

    db = str(tmp_path / "sessions.db")
    session_store.create_session(db, "活跃会话", "activesession01")
    old = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 8 * 86400))
    con = sqlite3.connect(db)
    con.execute("UPDATE sessions SET created_at=? WHERE id=?", (old, "activesession01"))
    con.commit()
    con.close()
    assert session_store.cleanup(db, 7) == 0
    assert session_store.get_session(db, "activesession01") is not None
