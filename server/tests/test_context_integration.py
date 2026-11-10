"""上下文组装的接口级验证：context 事件、引用清单、持久化与会话隔离。"""

import json
import pathlib
import shutil

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.indexing import store
from app.indexing.ingest import ingest_file
from app.main import create_app

FIXTURE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "data"
    / "corpus"
    / "fixture_术语混写_模拟维保手册.md"
)
SEED = pathlib.Path(__file__).resolve().parents[2] / "data" / "terms" / "seed.json"


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


def _ask(client, message: str, session_id: str | None = None) -> list[dict]:
    payload = {"message": message, "mode": "kb"}
    if session_id:
        payload["sessionId"] = session_id
    response = client.post("/api/chat", json=payload)
    assert response.status_code == 200
    return _parse_sse(response.text)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    terms = tmp_path / "terms"
    terms.mkdir()
    shutil.copy(SEED, terms / "seed.json")
    monkeypatch.setenv("INDOCQA_CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("INDOCQA_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("INDOCQA_SESSION_DB", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("INDOCQA_EMBED_BACKEND", "local")
    monkeypatch.setenv("INDOCQA_TERMS_FILE", str(terms / "terms.json"))
    get_settings.cache_clear()
    store._client.cache_clear()
    ingest_file(FIXTURE)
    yield TestClient(create_app())
    get_settings.cache_clear()
    store._client.cache_clear()


def test_context_event_and_manifest_persistence(client):
    events = _ask(client, "LUB 更换的保养周期是多少小时？")
    kinds = [event["event"] for event in events]
    assert "context" in kinds and "sources" in kinds and "done" in kinds
    context = next(event for event in events if event["event"] == "context")["data"]
    assert context["used"] <= context["budget"]
    assert context["template"] == "kb-answer@1"
    assert context["layers"]["evidence"] >= 1

    sources = next(event for event in events if event["event"] == "sources")["data"]["items"]
    assert sources
    session_id = next(event for event in events if event["event"] == "done")["data"]["sessionId"]
    detail = client.get(f"/api/sessions/{session_id}").json()["data"]
    assistant = [m for m in detail["messages"] if m["role"] == "assistant"][-1]
    manifest = assistant["contextManifest"]
    assert manifest and manifest["integrity"] is True
    assert set(manifest["citationMap"]) == {str(item["index"]) for item in sources}
    evidence = [unit for unit in manifest["units"] if unit["layer"] == "evidence"]
    assert evidence and all(unit["ref"]["chunkId"] for unit in evidence)
    assert any(unit["ref"]["docId"] for unit in evidence)


def test_context_mode_off_and_shadow(client, monkeypatch):
    monkeypatch.setenv("INDOCQA_CONTEXT_MODE", "off")
    get_settings.cache_clear()
    events = _ask(client, "LUB 更换的保养周期是多少小时？")
    assert "context" not in [event["event"] for event in events]
    assert any(event["event"] == "sources" and event["data"]["items"] for event in events)

    monkeypatch.setenv("INDOCQA_CONTEXT_MODE", "shadow")
    get_settings.cache_clear()
    events = _ask(client, "LUB 更换的保养周期是多少小时？")
    context = next(event for event in events if event["event"] == "context")["data"]
    assert context["shadow"] is True
    assert context["layers"]["evidence"] >= 1
    assert any(event["event"] == "sources" and event["data"]["items"] for event in events)


def test_persona_is_scoped_to_session(client):
    first = _ask(client, "以后都用中文回答。LUB 更换的保养周期是多少小时？")
    context_one = next(event for event in first if event["event"] == "context")["data"]
    assert context_one["layers"]["persona"] >= 1

    second = _ask(client, "LUB 更换的保养周期是多少小时？")
    context_two = next(event for event in second if event["event"] == "context")["data"]
    assert context_two["layers"]["persona"] == 0
