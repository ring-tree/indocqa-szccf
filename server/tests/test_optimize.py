"""优化项阶段测试：表号定位（O2）、后端校验（O3）、预检（O10）、引用补正（O4）、噪声簇（O7）。"""

import pathlib
import shutil

import pytest

from app.core.config import get_settings
from app.core.exceptions import AppError, BackendMismatchError
from app.indexing import store
from app.indexing.ingest import ingest_file
from app.indexing.manifest import Manifest
from app.indexing.preview import preview_document
from app.parsing.loader import load_document
from app.retrieval.terms import build_clusters

CORPUS = pathlib.Path(__file__).resolve().parents[2] / "data" / "corpus"
FIXTURE = CORPUS / "fixture_术语混写_模拟维保手册.md"
SEED = pathlib.Path(__file__).resolve().parents[2] / "data" / "terms" / "seed.json"


@pytest.fixture()
def tmp_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("INDOCQA_CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("INDOCQA_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("INDOCQA_SESSION_DB", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("INDOCQA_EMBED_BACKEND", "local")
    terms_dir = tmp_path / "terms"
    terms_dir.mkdir()
    shutil.copy(SEED, terms_dir / "seed.json")
    monkeypatch.setenv("INDOCQA_TERMS_FILE", str(terms_dir / "terms.json"))
    get_settings.cache_clear()
    store._client.cache_clear()
    yield get_settings()
    get_settings.cache_clear()
    store._client.cache_clear()


def test_md_table_caption_from_preceding_line(tmp_settings):
    doc = load_document(FIXTURE)
    tables = [t for t in doc.tables if "LUB" in " ".join(" ".join(r) for r in t.rows)]
    assert tables, "保养周期表未被解析"
    # fixture 题注无“表 X-X”编号，按设计不编造定位号（PDF 题注路径由集成场景覆盖）
    assert tables[0].table_no is None


def test_backend_mismatch_rejected(tmp_settings):
    ingest_file(FIXTURE, tmp_settings)
    manifest = Manifest(pathlib.Path(tmp_settings.chroma_dir) / "manifest.json")
    assert manifest.data["backend"] == store.get_embedder(tmp_settings).backend_id
    manifest.data["backend"] = "someone-else"
    manifest.save()
    with pytest.raises(BackendMismatchError):
        ingest_file(CORPUS / "无油静音空压机操作说明_厂商公开.pdf", tmp_settings)


def test_preview_fixture_ok(tmp_settings):
    report = preview_document(FIXTURE, tmp_settings)
    assert report["status"] == "ok"
    assert report["estimatedChunks"] > 0
    assert report["language"] == "zh"
    assert report["duplicated"] is False
    ingest_file(FIXTURE, tmp_settings)
    again = preview_document(FIXTURE, tmp_settings)
    assert again["duplicated"] is True


def test_preview_scanned_pdf_rejected(tmp_settings, tmp_path):
    """构造无文本层的场景：monkeypatch load_document 返回空段落文档。"""
    from app.indexing import preview as preview_mod
    from app.parsing.models import LoadedDoc

    fake = LoadedDoc(doc_id="x" * 16, filename="scan.pdf", paragraphs=[], tables=[], pages=30)
    original = preview_mod.load_document
    preview_mod.load_document = lambda p: fake
    try:
        report = preview_mod.preview_document(FIXTURE, tmp_settings)
        assert report["status"] == "rejected"
        assert any("扫描" in i["message"] for i in report["issues"])
    finally:
        preview_mod.load_document = original


def test_noise_cluster_filtered_by_frequency(tmp_settings):
    ingest_file(FIXTURE, tmp_settings)
    clusters = build_clusters(tmp_settings)
    noisy = [c for c in clusters if any("Running time" in m for m in c)]
    assert not noisy, "低频噪声簇未被过滤"
    assert any(any(m == "LUB" for m in c) for c in clusters), "语料抽取的有效簇被误杀"


@pytest.mark.asyncio
async def test_citation_retry_emits_trace(tmp_settings, monkeypatch):
    from app.graph import pipeline

    async def fake_answer_no_citation(question, hits, gateway, history=None):
        yield "没有任何引用标记的回答。"

    async def fake_answer_with_citation(question, hits, gateway, history=None):
        yield "补正后的回答 [1]。"

    ingest_file(FIXTURE, tmp_settings)
    monkeypatch.setattr(pipeline, "stream_answer", fake_answer_no_citation)
    events = []
    async for ev in pipeline.chat_stream(
        "LUB 更换的保养周期是多少小时？", "kb", settings=tmp_settings
    ):
        events.append(ev)
    stages = [e["data"].get("stage") for e in events if e["event"] == "trace"]
    assert "citation_retry" in stages, "缺引用未触发补正"
    tokens = "".join(e["data"]["delta"] for e in events if e["event"] == "token")
    assert "引用补正" in tokens, "补正内容未下发"

    monkeypatch.setattr(pipeline, "stream_answer", fake_answer_with_citation)
    events2 = []
    async for ev in pipeline.chat_stream(
        "LUB 更换的保养周期是多少小时？", "kb", settings=tmp_settings
    ):
        events2.append(ev)
    stages2 = [e["data"].get("stage") for e in events2 if e["event"] == "trace"]
    assert "citation_retry" not in stages2, "有引用不应触发补正"


def test_llm_error_codes_registered():
    from app.core.exceptions import BackendMismatchError

    err = BackendMismatchError("x")
    assert isinstance(err, AppError) and err.code == 40902
