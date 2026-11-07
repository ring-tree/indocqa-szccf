"""向量库存取：Chroma 持久化 + 父块文本侧车（parents.json）。

集合名携带嵌入后端标识，防止不同后端的向量混入同一空间。
"""

import json
import pathlib
from functools import lru_cache

from app.indexing.embedder import get_embedder
from app.indexing.manifest import Manifest
from app.parsing.models import ChildChunk, ParentChunk


@lru_cache
def _client(chroma_dir: str):
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    return chromadb.PersistentClient(
        path=chroma_dir, settings=ChromaSettings(anonymized_telemetry=False)
    )


def _collection(settings):
    embedder = get_embedder(settings)
    return _client(str(pathlib.Path(settings.chroma_dir))).get_or_create_collection(
        name=f"indocqa_{embedder.backend_id}",
        metadata={"hnsw:space": "cosine"},
    )


def _parents_path(settings) -> pathlib.Path:
    return pathlib.Path(settings.chroma_dir) / "parents.json"


def _load_parents(settings) -> dict:
    p = _parents_path(settings)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def _save_parents(settings, parents: dict) -> None:
    p = _parents_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(parents, ensure_ascii=False), encoding="utf-8")


def add_children(settings, children: list[ChildChunk], parents: list[ParentChunk], doc_name: str) -> None:
    from app.indexing.bm25_index import get_bm25

    embedder = get_embedder(settings)
    embeddings = embedder.embed([c.text for c in children])
    metadatas = []
    for c in children:
        meta: dict = {"doc_id": c.doc_id, "doc_name": doc_name, "section": c.section, "kind": c.kind}
        if c.page is not None:
            meta["page"] = c.page
        if c.table_no:
            meta["table_no"] = c.table_no
        metadatas.append(meta)
    _collection(settings).add(
        ids=[c.chunk_id for c in children],
        embeddings=embeddings,
        documents=[c.text for c in children],
        metadatas=metadatas,
    )
    store = _load_parents(settings)
    for p in parents:
        store[p.parent_id] = {"doc_id": p.doc_id, "section": p.section, "page_start": p.page_start, "text": p.text}
    _save_parents(settings, store)
    get_bm25(settings).update({c.chunk_id: f"{c.section}\n{c.text}" for c in children})


def query(settings, query_text: str, top_k: int) -> list[dict]:
    """返回 [{"sim", "text", "doc_id", ...}]；相似度 = 1 - 余弦距离。"""
    embedder = get_embedder(settings)
    emb = embedder.embed([query_text])[0]
    res = _collection(settings).query(
        query_embeddings=[emb], n_results=top_k, include=["documents", "metadatas", "distances"]
    )
    hits = []
    for cid, doc, meta, dist in zip(
        res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0]
    ):
        meta = dict(meta or {})
        hits.append(
            {
                "chunk_id": cid,
                "sim": 1.0 - float(dist),
                "text": doc,
                "doc_id": meta.get("doc_id", ""),
                "doc_name": meta.get("doc_name", ""),
                "section": meta.get("section", ""),
                "page": meta.get("page"),
                "kind": meta.get("kind", "text"),
                "table_no": meta.get("table_no"),
            }
        )
    return hits


def get_parent_text(settings, doc_id: str, section: str) -> str:
    """按 doc_id + 小节找父块文本，为生成提供章节级上下文。"""
    for p in _load_parents(settings).values():
        if p["doc_id"] == doc_id and p["section"] == section:
            return p["text"]
    return ""


def get_by_ids(settings, chunk_ids: list[str]) -> list[dict]:
    """按 chunk_id 批量取子块（BM25 命中但向量未召回时的回取），无相似度。"""
    if not chunk_ids:
        return []
    res = _collection(settings).get(ids=chunk_ids, include=["documents", "metadatas"])
    hits = []
    for cid, doc, meta in zip(res["ids"], res["documents"], res["metadatas"]):
        meta = dict(meta or {})
        hits.append(
            {
                "chunk_id": cid,
                "sim": None,
                "text": doc,
                "doc_id": meta.get("doc_id", ""),
                "doc_name": meta.get("doc_name", ""),
                "section": meta.get("section", ""),
                "page": meta.get("page"),
                "kind": meta.get("kind", "text"),
                "table_no": meta.get("table_no"),
            }
        )
    return hits


def delete_doc(settings, doc_id: str) -> None:
    from app.indexing.bm25_index import get_bm25

    _collection(settings).delete(where={"doc_id": doc_id})
    parents = {
        pid: p for pid, p in _load_parents(settings).items() if p["doc_id"] != doc_id
    }
    _save_parents(settings, parents)
    get_bm25(settings).remove_doc(doc_id)


def is_empty(settings) -> bool:
    return _collection(settings).count() == 0
