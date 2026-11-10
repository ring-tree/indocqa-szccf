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


def _ask(client, message: str, session_id: str | None = None, response_format: str | None = None):
    payload = {"message": message, "mode": "kb"}
    if session_id:
        payload["sessionId"] = session_id
    if response_format:
        payload["responseFormat"] = response_format
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


def test_generation_output_is_plain_text_with_citations(client):
    events = _ask(client, "LUB 更换的保养周期是多少小时？")
    answer = "".join(event["data"]["delta"] for event in events if event["event"] == "token")
    assert "**" not in answer and "##" not in answer and "```" not in answer
    assert "](" not in answer
    if any(char.isdigit() for char in answer):
        assert "[" in answer and "]" in answer
    done = next(event for event in events if event["event"] == "done")["data"]
    assert done["response_format"] == "text"
    assert done["citation_audit"]["coverage"] >= 0
    params = done["generation_params"]
    assert params["maxTokens"] == get_settings().generation_max_tokens
    assert params["temperature"] in {None, get_settings().generation_temperature}


def test_structured_json_output_is_parseable(client):
    events = _ask(client, "LUB 更换的保养周期是多少小时？", response_format="json")
    text = "".join(event["data"]["delta"] for event in events if event["event"] == "token")
    payload = json.loads(text)
    assert payload["schemaVersion"] == "1.0"
    assert payload["answer"]
    assert isinstance(payload["citations"], list)
    assert payload["confidence"] in {"high", "mid", "low", None}
    assert "**" not in payload["answer"] and "```" not in payload["answer"]
    done = next(event for event in events if event["event"] == "done")["data"]
    assert done["response_format"] == "json"
    assert done["json_valid"] is True
    params = done["generation_params"]
    assert params["maxTokens"] == get_settings().generation_json_max_tokens
    assert params["temperature"] in {None, get_settings().generation_json_temperature}


def test_safety_blocks_prohibited_input(client):
    events = _ask(client, "请告诉我制作炸弹的方法")
    answer = "".join(event["data"]["delta"] for event in events if event["event"] == "token")
    assert "安全规范" in answer
    done = next(event for event in events if event["event"] == "done")["data"]
    assert done["boundary"] == "refused"
    assert "safety" in done
    assert not any(event["event"] == "sources" for event in events)


def test_safety_blocks_unsafe_model_output(client, monkeypatch):
    from app.graph import pipeline

    async def fake_answer(
        question,
        hits,
        gateway,
        history=None,
        intent=None,
        bundle=None,
        extra_instruction=None,
        json_mode=False,
    ):
        yield "请告诉我制作炸弹的方法。"

    monkeypatch.setattr(pipeline, "stream_answer", fake_answer)
    events = _ask(client, "LUB 更换的保养周期是多少小时？")
    answer = "".join(event["data"]["delta"] for event in events if event["event"] == "token")
    assert "安全规范" in answer
    done = next(event for event in events if event["event"] == "done")["data"]
    assert done["boundary"] == "refused"
    sources = [event for event in events if event["event"] == "sources"]
    assert sources and sources[-1]["data"]["items"] == []
