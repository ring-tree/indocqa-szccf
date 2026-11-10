"""批量移动/撤销与 AI 分类：操作日志、撤销有效期、无模型门禁、规则候选。"""

import pathlib

import pytest
from fastapi.testclient import TestClient

from app.classification import service as classify_service
from app.core.config import get_settings
from app.core.exceptions import AIUnavailableError, ParamError
from app.indexing import store
from app.indexing.ingest import ingest_file
from app.indexing.manifest import Manifest
from app.indexing.moves import batch_move, undo_move
from app.indexing.preview import preview_document
from app.main import create_app

CORPUS = pathlib.Path(__file__).resolve().parents[2] / "data" / "corpus"
FIXTURE = CORPUS / "fixture_术语混写_模拟维保手册.md"
SEED = pathlib.Path(__file__).resolve().parents[2] / "data" / "terms" / "seed.json"


@pytest.fixture()
def tmp_settings(tmp_path, monkeypatch):
    import shutil

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
    yield get_settings()
    get_settings.cache_clear()
    store._client.cache_clear()


def _md(path: pathlib.Path, title: str, body: str) -> pathlib.Path:
    path.write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
    return path


def _two_docs(settings, tmp_path):
    a = _md(tmp_path / "a.md", "螺杆机维护", "LUB 更换周期 4000 小时。")
    b = _md(tmp_path / "b.md", "可靠性指标", "MTBF 8000 小时。")
    first = ingest_file(a, settings, folder="设备手册/螺杆空压机")
    second = ingest_file(b, settings, folder="评测夹具")
    return first["docId"], second["docId"]


def test_batch_move_and_undo(tmp_settings, tmp_path):
    first, second = _two_docs(tmp_settings, tmp_path)
    result = batch_move(tmp_settings, [first, second], "行业标准/压缩空气系统")
    assert sorted(result["moved"]) == sorted([first, second])
    assert result["operationId"]
    manifest = Manifest(Manifest.path_for(tmp_settings))
    assert manifest.get(first)["folder"] == "行业标准/压缩空气系统"
    assert manifest.get(second)["folder"] == "行业标准/压缩空气系统"

    undo = undo_move(tmp_settings, result["operationId"])
    assert sorted(undo["restored"]) == sorted([first, second])
    manifest = Manifest(Manifest.path_for(tmp_settings))
    assert manifest.get(first)["folder"] == "设备手册/螺杆空压机"
    assert manifest.get(second)["folder"] == "评测夹具"

    with pytest.raises(ParamError):
        undo_move(tmp_settings, result["operationId"])


def test_batch_move_skips_same_folder_and_reports_missing(tmp_settings, tmp_path):
    first, _ = _two_docs(tmp_settings, tmp_path)
    result = batch_move(tmp_settings, [first, "missing"], "设备手册/螺杆空压机")
    assert result["skipped"] == [first]
    assert result["failed"][0]["docId"] == "missing"
    assert result["operationId"] is None


def test_move_api_creates_undo_entry(tmp_settings, tmp_path):
    first, _ = _two_docs(tmp_settings, tmp_path)
    client = TestClient(create_app())
    response = client.post(
        "/api/kb/documents/batch-move",
        json={"docIds": [first], "folder": "行业标准/压缩空气系统"},
    )
    assert response.status_code == 200
    operation_id = response.json()["data"]["operationId"]
    assert operation_id

    undo = client.post(f"/api/kb/moves/{operation_id}/undo")
    assert undo.status_code == 200
    assert undo.json()["data"]["restored"] == [first]


def test_classify_gate_without_model(tmp_settings, monkeypatch):
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
    fresh = get_settings()

    with pytest.raises(AIUnavailableError):
        __import__("asyncio").run(
            classify_service.classify_text(fresh, "a.pdf", "螺杆空压机维护手册")
        )

    client = TestClient(create_app())
    response = client.post("/api/kb/classify", json={"docId": "any"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == 40904
    assert response.json()["error"]["message"] == "无可用模型"


def test_preview_classification_reports_unavailable_without_model(tmp_settings, monkeypatch):
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
    report = preview_document(FIXTURE, get_settings(), classify=True)
    assert report["classification"]["available"] is False
    assert report["classification"]["reason"] == "no_model"


def test_rule_candidates_prefer_matching_folder(tmp_settings):
    candidates = classify_service._build_candidates(
        ["设备手册/螺杆空压机", "行业标准/压缩空气系统", "评测夹具"],
        "螺杆式空压机操作手册.pdf",
        "螺杆压缩机维护与保养",
        {},
        {},
    )
    assert candidates[0]["path"] == "设备手册/螺杆空压机"
    assert candidates[0]["confidence"] >= 0.9
    assert "螺杆" in candidates[0]["reason"] or "型号" in candidates[0]["reason"]
