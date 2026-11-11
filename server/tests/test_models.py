"""输出层模型清单与 profile 选择。"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.llm import LLMGateway
from app.main import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INDOCQA_SESSION_DB", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("INDOCQA_LLM_BASE_URL", "http://primary.example/v1")
    monkeypatch.setenv("INDOCQA_LLM_API_KEY", "sk-test-primary")
    monkeypatch.setenv("INDOCQA_LLM_MODEL", "model-primary")
    monkeypatch.setenv("INDOCQA_LLM_BACKUP_BASE_URL", "http://backup.example/v1")
    monkeypatch.setenv("INDOCQA_LLM_BACKUP_API_KEY", "sk-test-backup")
    monkeypatch.setenv("INDOCQA_LLM_BACKUP_MODEL", "model-backup")
    get_settings.cache_clear()
    yield TestClient(create_app())
    get_settings.cache_clear()


def test_models_endpoint_lists_profiles_without_secrets(client):
    data = client.get("/api/models").json()["data"]
    assert [p["id"] for p in data] == ["primary", "backup"]
    assert data[0]["model"] == "model-primary"
    body = str(data)
    assert "sk-test" not in body and "example" not in body


def test_gateway_profile_order(monkeypatch):
    monkeypatch.setenv("INDOCQA_LLM_BASE_URL", "http://primary.example/v1")
    monkeypatch.setenv("INDOCQA_LLM_API_KEY", "sk-test-primary")
    monkeypatch.setenv("INDOCQA_LLM_MODEL", "model-primary")
    monkeypatch.setenv("INDOCQA_LLM_BACKUP_BASE_URL", "http://backup.example/v1")
    monkeypatch.setenv("INDOCQA_LLM_BACKUP_API_KEY", "sk-test-backup")
    monkeypatch.setenv("INDOCQA_LLM_BACKUP_MODEL", "model-backup")
    get_settings.cache_clear()
    try:
        gateway = LLMGateway(get_settings(), "backup")
        assert [p["id"] for p in gateway._ordered()] == ["backup", "primary"]
    finally:
        get_settings.cache_clear()


def test_placeholder_backup_is_not_listed(tmp_path, monkeypatch):
    monkeypatch.setenv("INDOCQA_SESSION_DB", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("INDOCQA_LLM_BASE_URL", "http://primary.example/v1")
    monkeypatch.setenv("INDOCQA_LLM_API_KEY", "sk-real-primary-key")
    monkeypatch.setenv("INDOCQA_LLM_MODEL", "model-primary")
    monkeypatch.setenv("INDOCQA_LLM_BACKUP_BASE_URL", "http://backup.example/v1")
    monkeypatch.setenv("INDOCQA_LLM_BACKUP_API_KEY", "sk-your-backup-key-here")
    monkeypatch.setenv("INDOCQA_LLM_BACKUP_MODEL", "model-backup")
    get_settings.cache_clear()
    try:
        c = TestClient(create_app())
        data = c.get("/api/models").json()["data"]
        assert [p["id"] for p in data] == ["primary"]
    finally:
        get_settings.cache_clear()


def test_chat_rejects_unknown_profile(client):
    r = client.post(
        "/api/chat",
        json={"message": "你好", "mode": "direct", "profile": "nope"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == 40001


def test_models_endpoint_empty_when_not_configured(tmp_path, monkeypatch):
    for var in ("INDOCQA_LLM_BASE_URL", "INDOCQA_LLM_BACKUP_BASE_URL"):
        monkeypatch.setenv(var, "http://disabled.invalid/v1")
    for var in ("INDOCQA_LLM_API_KEY", "INDOCQA_LLM_BACKUP_API_KEY"):
        monkeypatch.setenv(var, "x")
    for var in ("INDOCQA_LLM_MODEL", "INDOCQA_LLM_BACKUP_MODEL"):
        monkeypatch.setenv(var, "disabled")
    monkeypatch.setenv("INDOCQA_SESSION_DB", str(tmp_path / "sessions.db"))
    get_settings.cache_clear()
    try:
        c = TestClient(create_app())
        assert c.get("/api/models").json()["data"] == []
        r = c.post("/api/chat", json={"message": "你好", "mode": "direct"})
        assert r.status_code == 502 and r.json()["error"]["code"] == 42901
    finally:
        get_settings.cache_clear()
