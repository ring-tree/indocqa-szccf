"""知识库管理新增能力：多目录范围、移动保持向量、入库进度、健康向量化模式。"""

import pathlib
import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.exceptions import ParamError
from app.indexing import store
from app.indexing.folders import normalize_display_names
from app.indexing.ingest import ingest_file
from app.indexing.manifest import Manifest
from app.indexing.moves import batch_move, undo_move
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
        "MTBF 8000 小时",
        top_k=6,
        settings=tmp_settings,
        folders=["设备手册", "评测夹具"],
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
    ingest_file(a, tmp_settings, folder="设备手册/螺杆")
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

    batch_move(tmp_settings, [doc_id], "行业标准/压缩空气系统")
    assert collection.count() == count_before
    vector_after = store.doc_profile(tmp_settings, doc_id)["vector"]
    assert vector_before and vector_after
    assert max(abs(x - y) for x, y in zip(vector_before, vector_after)) < 1e-6

    hits = store.query(tmp_settings, "LUB 4000", 5, folder="行业标准/压缩空气系统")
    assert any(hit["doc_id"] == doc_id for hit in hits)


def test_batch_move_mixed_target_stats(tmp_settings, tmp_path):
    a = _md(tmp_path / "a.md", "A", "LUB 4000 小时。")
    b = _md(tmp_path / "b.md", "B", "MTBF 8000 小时。")
    first = ingest_file(a, tmp_settings, folder="设备手册/螺杆")["docId"]
    second = ingest_file(b, tmp_settings, folder="行业标准/压缩空气系统")["docId"]
    result = batch_move(tmp_settings, [first, second], "行业标准/压缩空气系统")
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
        folder="设备手册/螺杆",
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
    result = ingest_file(path, tmp_settings, stored_name=path.name, folder="设备手册/螺杆")
    assert result["filename"] == path.name
    manifest = Manifest(Manifest.path_for(tmp_settings))
    assert manifest.get(result["docId"])["filename"] == path.name

    assert normalize_display_names(tmp_settings) == 1
    manifest = Manifest(Manifest.path_for(tmp_settings))
    assert manifest.get(result["docId"])["filename"] == "现场手册.md"
    hits = store.query(tmp_settings, "LUB 4000", 5)
    assert any(hit["doc_name"] == "现场手册.md" for hit in hits)


def test_non_leaf_move_targets_rejected(tmp_settings, tmp_path):
    doc = _md(tmp_path / "a.md", "维护", "LUB 4000 小时。")
    other = _md(tmp_path / "b.md", "标准", "行业标准资料。")
    doc_id = ingest_file(doc, tmp_settings, folder="设备手册/螺杆")["docId"]
    ingest_file(other, tmp_settings, folder="行业标准/压缩空气系统")
    for target in ("设备手册", "行业标准"):
        with pytest.raises(ParamError, match="最末级目录"):
            batch_move(tmp_settings, [doc_id], target)


def test_batch_move_multiple_to_leaf(tmp_settings, tmp_path):
    a = _md(tmp_path / "a.md", "A", "LUB 4000 小时。")
    b = _md(tmp_path / "b.md", "B", "MTBF 8000 小时。")
    first = ingest_file(a, tmp_settings, folder="评测夹具")["docId"]
    second = ingest_file(b, tmp_settings, folder="行业标准/压缩空气系统")["docId"]
    result = batch_move(tmp_settings, [first, second], "设备手册/螺杆空压机")
    assert sorted(result["moved"]) == sorted([first, second])
    assert result["failed"] == []
    manifest = Manifest(Manifest.path_for(tmp_settings))
    assert manifest.get(first)["folder"] == "设备手册/螺杆空压机"
    assert manifest.get(second)["folder"] == "设备手册/螺杆空压机"


def test_batch_move_all_documents_from_root(tmp_settings, tmp_path):
    """根目录全选：多来源且含已在目标目录的文档，其余文档仍应移动。"""
    specs = [
        ("设备手册/螺杆空压机", "螺杆"),
        ("设备手册/无油空压机", "无油"),
        ("行业标准/压缩空气系统", "标准"),
        ("评测夹具", "夹具"),
    ]
    ids = []
    for index, (folder, title) in enumerate(specs):
        path = _md(tmp_path / f"doc{index}.md", title, f"LUB {index} 小时。")
        ids.append(ingest_file(path, tmp_settings, folder=folder)["docId"])

    result = batch_move(tmp_settings, ids, "设备手册/螺杆空压机")
    assert result["skipped"] == [ids[0]]
    assert sorted(result["moved"]) == sorted(ids[1:])
    assert result["failed"] == []


def test_batch_move_same_folder_is_noop(tmp_settings, tmp_path):
    path = _md(tmp_path / "same.md", "同目录", "LUB 4000 小时。")
    doc_id = ingest_file(path, tmp_settings, folder="评测夹具")["docId"]

    result = batch_move(tmp_settings, [doc_id], "评测夹具")
    assert result["moved"] == []
    assert result["skipped"] == [doc_id]
    assert result["operationId"] is None


def test_batch_move_cross_branch_and_depth(tmp_settings, tmp_path):
    path = _md(tmp_path / "cross.md", "跨层级", "LUB 4000 小时。")
    doc_id = ingest_file(path, tmp_settings, folder="设备手册/螺杆空压机")["docId"]

    deeper = batch_move(tmp_settings, [doc_id], "行业标准/压缩空气系统/能效")
    assert deeper["moved"] == [doc_id]
    shallow = batch_move(tmp_settings, [doc_id], "评测夹具")
    assert shallow["moved"] == [doc_id]
    sibling = batch_move(tmp_settings, [doc_id], "设备手册/无油空压机")
    assert sibling["moved"] == [doc_id]


def test_batch_move_mixed_document_types(tmp_settings, tmp_path):
    md = _md(tmp_path / "mixed.md", "Markdown", "LUB 4000 小时。")
    txt = tmp_path / "mixed.txt"
    txt.write_text("MTBF 8000 小时。", encoding="utf-8")
    md_id = ingest_file(md, tmp_settings, folder="评测夹具")["docId"]
    txt_id = ingest_file(txt, tmp_settings, folder="行业标准/压缩空气系统")["docId"]

    result = batch_move(tmp_settings, [md_id, txt_id], "设备手册/无油空压机")
    assert sorted(result["moved"]) == sorted([md_id, txt_id])
    assert result["failed"] == []


def test_batch_move_undo_restores_mixed_sources(tmp_settings, tmp_path):
    first = ingest_file(
        _md(tmp_path / "u1.md", "U1", "LUB 4000 小时。"), tmp_settings, folder="评测夹具"
    )["docId"]
    second = ingest_file(
        _md(tmp_path / "u2.md", "U2", "MTBF 8000 小时。"),
        tmp_settings,
        folder="行业标准/压缩空气系统",
    )["docId"]

    result = batch_move(tmp_settings, [first, second], "设备手册/无油空压机")
    assert sorted(result["moved"]) == sorted([first, second])
    undone = undo_move(tmp_settings, result["operationId"])
    assert sorted(undone["restored"]) == sorted([first, second])

    manifest = Manifest(Manifest.path_for(tmp_settings))
    assert manifest.get(first)["folder"] == "评测夹具"
    assert manifest.get(second)["folder"] == "行业标准/压缩空气系统"
    with pytest.raises(ParamError, match="已撤销"):
        undo_move(tmp_settings, result["operationId"])


def test_batch_move_reports_missing_documents(tmp_settings, tmp_path):
    path = _md(tmp_path / "exists.md", "存在", "LUB 4000 小时。")
    doc_id = ingest_file(path, tmp_settings, folder="评测夹具")["docId"]

    result = batch_move(tmp_settings, [doc_id, "missing-doc"], "设备手册/无油空压机")
    assert result["moved"] == [doc_id]
    assert result["failed"] == [{"docId": "missing-doc", "reason": "文档不存在"}]


def test_create_folder_creates_all_levels(tmp_settings):
    import asyncio

    from app.api.kb import FolderCreate, create_folder

    asyncio.run(create_folder(FolderCreate(path="一级/二级/三级/四级")))
    folders = Manifest(Manifest.path_for(tmp_settings)).folders()
    for path in ("一级", "一级/二级", "一级/二级/三级", "一级/二级/三级/四级"):
        assert path in folders


def test_delete_folder_structure_up_keeps_children(tmp_settings, tmp_path):
    import asyncio

    from app.api.kb import delete_folder

    first = ingest_file(
        _md(tmp_path / "s1.md", "S1", "LUB 4000 小时。"), tmp_settings, folder="事业部/设备/螺杆"
    )["docId"]
    second = ingest_file(
        _md(tmp_path / "s2.md", "S2", "MTBF 8000 小时。"), tmp_settings, folder="事业部/设备/无油"
    )["docId"]

    asyncio.run(delete_folder("事业部/设备", mode="move"))
    manifest = Manifest(Manifest.path_for(tmp_settings))
    assert manifest.get(first)["folder"] == "事业部/螺杆"
    assert manifest.get(second)["folder"] == "事业部/无油"
    folders = manifest.folders()
    assert "事业部/设备" not in folders
    assert "事业部/螺杆" in folders and "事业部/无油" in folders


def test_delete_folder_structure_up_at_root(tmp_settings, tmp_path):
    import asyncio

    from app.api.kb import delete_folder

    doc_id = ingest_file(
        _md(tmp_path / "r.md", "R", "LUB 4000 小时。"), tmp_settings, folder="设备手册/螺杆"
    )["docId"]

    asyncio.run(delete_folder("设备手册", mode="move"))
    manifest = Manifest(Manifest.path_for(tmp_settings))
    assert manifest.get(doc_id)["folder"] == "螺杆"
    assert "设备手册" not in manifest.folders()
    assert "螺杆" in manifest.folders()


def test_delete_folder_cascade_removes_subtree(tmp_settings, tmp_path):
    import asyncio

    from app.api.kb import delete_folder

    ingest_file(
        _md(tmp_path / "c1.md", "C1", "LUB 4000 小时。"), tmp_settings, folder="事业部/设备/螺杆"
    )
    ingest_file(
        _md(tmp_path / "c2.md", "C2", "MTBF 8000 小时。"), tmp_settings, folder="事业部/设备/无油"
    )

    asyncio.run(delete_folder("事业部/设备", mode="delete"))
    manifest = Manifest(Manifest.path_for(tmp_settings))
    assert manifest.documents() == {}
    folders = manifest.folders()
    assert not any(item == "事业部/设备" or item.startswith("事业部/设备/") for item in folders)


def test_ingest_to_non_leaf_rejected(tmp_settings, tmp_path):
    first = _md(tmp_path / "a.md", "A", "LUB 4000 小时。")
    ingest_file(first, tmp_settings, folder="设备手册/螺杆")
    second = _md(tmp_path / "b.md", "B", "MTBF 8000 小时。")
    with pytest.raises(ParamError, match="最末级目录"):
        ingest_file(second, tmp_settings, folder="设备手册")


def test_create_subfolder_under_doc_folder_rejected(tmp_settings):
    from app.api.kb import FolderCreate, create_folder

    ingest_file(
        pathlib.Path(SEED).parent.parent / "corpus" / "fixture_术语混写_模拟维保手册.md",
        tmp_settings,
    )
    import asyncio

    with pytest.raises(ParamError, match="已有文档"):
        asyncio.run(create_folder(FolderCreate(path="未分类/子目录")))


def test_new_folder_proposal_when_candidates_low(tmp_settings, tmp_path):
    from app.classification import service as classify_service

    a = _md(tmp_path / "a.md", "A", "LUB 4000 小时。")
    b = _md(tmp_path / "b.md", "B", "MTBF 8000 小时。")
    ingest_file(a, tmp_settings, folder="设备手册/螺杆")
    ingest_file(b, tmp_settings, folder="评测夹具")
    manifest = Manifest(Manifest.path_for(tmp_settings))
    candidates = classify_service._build_candidates(
        ["设备手册/螺杆", "评测夹具"], "液压泵维护手册.pdf", "液压泵维护说明", {}, {}
    )
    assert candidates and all(item["confidence"] < 0.6 for item in candidates)
    proposal = classify_service._propose_new_folder(manifest, "液压泵维护手册.pdf", candidates)
    assert proposal is not None
    assert proposal["path"] == "液压泵维护"
    assert proposal["path"] not in manifest.folders()
    assert proposal["name"] == "液压泵维护"


def test_classification_candidates_are_leaf_only():
    from app.classification import service as classify_service

    candidates = classify_service._build_candidates(
        ["设备手册", "设备手册/螺杆空压机", "评测夹具"],
        "螺杆式空压机操作手册.pdf",
        "螺杆压缩机维护",
        {},
        {},
    )
    assert candidates
    assert all(item["path"] != "设备手册" for item in candidates)


def test_health_reports_embedding_mode(tmp_settings):
    client = TestClient(create_app())
    body = client.get("/api/health").json()
    assert body["embedding"]["backend"].startswith("fe-")
    assert body["embedding"]["mode"] == "local"
    assert body["embedding"]["model"]
    assert Manifest(Manifest.path_for(tmp_settings)).documents() == {}
