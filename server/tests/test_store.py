"""索引与入库阶段测试（T1.3）：幂等、检索命中、删除（使用临时目录与默认嵌入后端）。"""

import pathlib

import pytest

from app.core.config import get_settings
from app.indexing import store
from app.indexing.ingest import ingest_file
from app.indexing.manifest import Manifest
from app.parsing.loader import load_document
from app.parsing.chunker import build_chunks

CORPUS = pathlib.Path(__file__).resolve().parents[2] / "data" / "corpus"
FIXTURE = CORPUS / "fixture_术语混写_模拟维保手册.md"


@pytest.fixture()
def tmp_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("INDOCQA_CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("INDOCQA_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("INDOCQA_EMBED_BACKEND", "default")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()
    store._client.cache_clear()


def test_ingest_idempotent(tmp_settings):
    first = ingest_file(FIXTURE, tmp_settings)
    second = ingest_file(FIXTURE, tmp_settings)
    assert first["duplicated"] is False and first["chunks"] > 0
    assert second["duplicated"] is True
    assert second["docId"] == first["docId"]
    docs, chunks = Manifest(pathlib.Path(tmp_settings.chroma_dir) / "manifest.json").stats()
    assert docs == 1 and chunks == first["chunks"]


def test_search_hits_with_source_meta(tmp_settings):
    ingest_file(FIXTURE, tmp_settings)
    hits = store.query(tmp_settings, "MTBF 平均无故障时间 是什么", top_k=5)
    assert hits, "检索无命中"
    top = max(hits, key=lambda h: h["sim"])
    assert top["doc_name"] == FIXTURE.name
    assert top["doc_id"] and top["section"]


def test_delete_doc_removes_chunks(tmp_settings):
    result = ingest_file(FIXTURE, tmp_settings)
    store.delete_doc(tmp_settings, result["docId"])
    assert store.is_empty(tmp_settings) is True
    manifest = Manifest(pathlib.Path(tmp_settings.chroma_dir) / "manifest.json")
    manifest.remove(result["docId"])
    manifest.save()
    assert manifest.stats()[0] == 0


def test_parent_text_lookup(tmp_settings):
    ingest_file(FIXTURE, tmp_settings)
    doc = load_document(FIXTURE)
    children, _ = build_chunks(doc)
    sample = children[0]
    text = store.get_parent_text(tmp_settings, sample.doc_id, sample.section)
    assert sample.text in text


def test_hybrid_search_ranks_table_top(tmp_settings):
    from app.retrieval.searcher import search

    ingest_file(FIXTURE, tmp_settings)
    hits = search("LUB 更换的保养周期是多少小时？", settings=tmp_settings)
    assert hits, "混合检索无命中"
    assert any("4000 h" in h.text for h in hits[:3]), "保养周期表未进前三"


def test_bm25_prefix_code_match(tmp_settings):
    from app.retrieval.searcher import search

    ingest_file(FIXTURE, tmp_settings)
    hits = search("E05 故障是什么含义", settings=tmp_settings)
    assert any("排气温度过高" in h.text for h in hits[:3])
