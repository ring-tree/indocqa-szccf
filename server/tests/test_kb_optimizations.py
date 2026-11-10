"""知识库管理新增能力：多目录范围、移动保持向量、入库进度、健康向量化模式。"""

import pathlib
import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.indexing import store
from app.indexing.folders import normalize_display_names
from app.indexing.ingest import ingest_file
from app.indexing.manifest import Manifest
from app.indexing.moves import batch_move
from app.main import create_app
from app.retrieval.searcher import search

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


def test_multi_folder_scope_search(tmp_settings, tmp_path):
    a = _md(tmp_path / "a.md", "螺杆维护", "LUB 更换周期为 4000 小时。")
    b = _md(tmp_path / "b.md", "可靠性", "MTBF 平均无故障时间为 8000 小时。")
    c = _md(tmp_path / "c.md", "储气罐", "储气罐压力达到 0.8 MPa 时压力开关关闭。")
    ingest_file(a, tmp_settings, folder="设备手册/螺杆")
    ingest_file(b, tmp_settings, folder="评测夹具")
    ingest_file(c, tmp_settings, folder="行业标准/储气")

    hits = search(
        "MTBF 8000 小时", top_k=6, settings=tmp_settings, folders=["设备手册", "评测夹具"]
    )
    assert hits
    assert all(h.folder in {"设备手册/螺杆", "评测夹具"} for h in hits)
    assert any(h.doc_name == "b.md" for h in hits)

    only_c = search("0.8 MPa", top_k=6, settings=tmp_settings, folders=["行业标准"])
    assert only_c and all(h.folder == "行业标准/储气" for h in only_c)


@pytest.mark.asyncio
async def test_multi_folder_scope_graph(tmp_settings, tmp_path):
    from app.graph.graph import run_retrieval

    a = _md(tmp_path / "a.md", "螺杆维护", "LUB 更换周期为 4000 小时。")
    b = _md(tmp_path / "b.md", "可靠性", "MTBF 平均无故障时间为 8000 小时。")
    ingest_file(a, tmp_settings, folder="设备手册")
    ingest_file(b, tmp_settings, folder="评测夹具")
    result = await run_retrieval("MTBF 是什么", "adaptive", tmp_settings, folders=["评测夹具"])
    assert result["hits"]
    assert all(hit.folder == "评测夹具" for hit in result["hits"])


def test_move_keeps_vectors_without_rebuild(tmp_settings, tmp_path):
    doc = _md(tmp_path / "a.md", "螺杆维护", "LUB 更换周期为 4000 小时。")
    result = ingest_file(doc, tmp_settings, folder="设备手册/螺杆")
    doc_id = result["docId"]
    collection = store._collection(tmp_settings)
    count_before = collection.count()
    vector_before = store.doc_profile(tmp_settings, doc_id)["vector"]

    batch_move(tmp_settings, [doc_id], "行业标准")
    assert collection.count() == count_before
    vector_after = store.doc_profile(tmp_settings, doc_id)["vector"]
    assert vector_before and vector_after
    assert max(abs(x - y) for x, y in zip(vector_before, vector_after)) < 1e-6

    hits = store.query(tmp_settings, "LUB 4000", 5, folder="行业标准")
    assert any(hit["doc_id"] == doc_id for hit in hits)


def test_batch_move_mixed_target_stats(tmp_settings, tmp_path):
    a = _md(tmp_path / "a.md", "A", "LUB 4000 小时。")
    b = _md(tmp_path / "b.md", "B", "MTBF 8000 小时。")
    first = ingest_file(a, tmp_settings, folder="设备手册")["docId"]
    second = ingest_file(b, tmp_settings, folder="行业标准")["docId"]
    result = batch_move(tmp_settings, [first, second], "行业标准")
    assert result["moved"] == [first]
    assert result["skipped"] == [second]
    assert result["skippedDetails"] == [{"docId": second, "reason": "已在目标目录"}]
    assert result["failed"] == []


def test_ingest_progress_callbacks(tmp_settings, tmp_path):
    doc = _md(tmp_path / "a.md", "进度", "LUB 更换周期为 4000 小时。" * 20)
    events = []
    result = ingest_file(
        doc,
        tmp_settings,
        folder="设备手册",
        progress=lambda stage, done, total: events.append((stage, done, total)),
    )
    assert result["chunks"] > 0
    stages = [event[0] for event in events]
    assert "parsing" in stages and "chunking" in stages and "embedding" in stages
    assert any(stage == "embedding" and total > 0 for stage, _, total in events)
    assert stages[-1] == "done"


def test_ingest_job_api_progress(tmp_settings, tmp_path):
    doc = _md(tmp_path / "job.md", "后台入库", "LUB 更换周期为 4000 小时。")
    client = TestClient(create_app())
    with open(doc, "rb") as fh:
        response = client.post(
            "/api/kb/ingest/jobs",
            files={"file": ("job.md", fh, "text/markdown")},
            data={"folder": "设备手册/后台"},
        )
    assert response.status_code == 200
    job_id = response.json()["data"]["jobId"]
    for _ in range(100):
        job = client.get(f"/api/kb/ingest/jobs/{job_id}").json()["data"]
        if job["status"] in {"done", "error"}:
            break
        time.sleep(0.1)
    assert job["status"] == "done", job
    assert job["percent"] == 100
    assert job["totalDocs"] == 1
    assert job["result"]["chunks"] > 0
    assert job["result"]["filename"] == "job.md"
    assert client.get("/api/kb/ingest/jobs/not-exist").status_code == 404


def test_upload_display_name_and_normalize(tmp_settings, tmp_path):
    path = _md(tmp_path / "abcd1234_现场手册.md", "现场手册", "LUB 更换周期为 4000 小时。")
    result = ingest_file(path, tmp_settings, stored_name=path.name, folder="设备手册")
    assert result["filename"] == path.name
    manifest = Manifest(Manifest.path_for(tmp_settings))
    assert manifest.get(result["docId"])["filename"] == path.name

    assert normalize_display_names(tmp_settings) == 1
    manifest = Manifest(Manifest.path_for(tmp_settings))
    assert manifest.get(result["docId"])["filename"] == "现场手册.md"
    hits = store.query(tmp_settings, "LUB 4000", 5)
    assert any(hit["doc_name"] == "现场手册.md" for hit in hits)


def test_health_reports_embedding_mode(tmp_settings):
    client = TestClient(create_app())
    body = client.get("/api/health").json()
    assert body["embedding"]["backend"].startswith("fe-")
    assert body["embedding"]["mode"] == "local"
    assert body["embedding"]["model"]
    assert Manifest(Manifest.path_for(tmp_settings)).documents() == {}
