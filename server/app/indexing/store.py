"""向量库存取：Chroma 持久化 + 父块文本侧车（parents.json）。

集合名携带嵌入后端标识，防止不同后端的向量混入同一空间；文档目录写入
Chroma 元数据（folder），检索按目录子树过滤。
"""

import json
import pathlib
from collections import OrderedDict

from app.indexing.embedder import get_embedder
from app.indexing.folders import DEFAULT_FOLDER
from app.indexing.lock import write_guard
from app.parsing.models import ChildChunk, ParentChunk

_EMBED_CACHE: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
_EMBED_CACHE_MAX = 256
_collections: dict[tuple[str, str], object] = {}
_parents_cache: dict[str, tuple[float, dict]] = {}


def _client(chroma_dir: str):
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    if not hasattr(_client, "_instances"):
        _client._instances = {}
    if chroma_dir not in _client._instances:
        _client._instances[chroma_dir] = chromadb.PersistentClient(
            path=chroma_dir, settings=ChromaSettings(anonymized_telemetry=False)
        )
    return _client._instances[chroma_dir]


def _client_cache_clear() -> None:
    """测试用：清空客户端与集合缓存（兼容原有 `store._client.cache_clear()` 调用）。"""
    if hasattr(_client, "_instances"):
        _client._instances.clear()
    _collections.clear()
    _parents_cache.clear()
    _EMBED_CACHE.clear()


_client.cache_clear = _client_cache_clear


def _collection(settings):
    embedder = get_embedder(settings)
    key = (str(pathlib.Path(settings.chroma_dir)), embedder.backend_id)
    if key not in _collections:
        _collections[key] = _client(key[0]).get_or_create_collection(
            name=f"indocqa_{embedder.backend_id}",
            metadata={
                "hnsw:space": "cosine",
                # 小库默认参数在增删/重启后偶发漏召回；提高构图与查询 ef 换取稳定召回
                "hnsw:construction_ef": 200,
                "hnsw:search_ef": 200,
                "hnsw:M": 32,
            },
        )
    return _collections[key]


def _parents_path(settings) -> pathlib.Path:
    return pathlib.Path(settings.chroma_dir) / "parents.json"


def _load_parents(settings) -> dict:
    p = _parents_path(settings)
    if not p.exists():
        return {}
    mtime = p.stat().st_mtime
    hit = _parents_cache.get(str(p))
    if hit and hit[0] == mtime:
        return hit[1]
    data = json.loads(p.read_text(encoding="utf-8"))
    _parents_cache[str(p)] = (mtime, data)
    return data


def _save_parents(settings, parents: dict) -> None:
    p = _parents_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(parents, ensure_ascii=False), encoding="utf-8")
    _parents_cache[str(p)] = (p.stat().st_mtime, parents)


def query_embedding(settings, text: str) -> list[float]:
    """查询向量带进程内 LRU（256 条）：重复/相近问题免去一次嵌入调用。"""
    embedder = get_embedder(settings)
    key = (embedder.backend_id, text)
    if key in _EMBED_CACHE:
        _EMBED_CACHE.move_to_end(key)
        return _EMBED_CACHE[key]
    vector = embedder.query_embed([text])[0]
    _EMBED_CACHE[key] = vector
    if len(_EMBED_CACHE) > _EMBED_CACHE_MAX:
        _EMBED_CACHE.popitem(last=False)
    return vector


def add_children(
    settings,
    children: list[ChildChunk],
    parents: list[ParentChunk],
    doc_name: str,
    folder: str = DEFAULT_FOLDER,
    progress=None,
) -> None:
    from app.indexing.bm25_index import get_bm25

    with write_guard(settings):
        embedder = get_embedder(settings)
        texts = [c.text for c in children]
        embeddings: list[list[float]] = []
        batch_size = 32
        total = len(texts)
        for start in range(0, total, batch_size):
            batch = texts[start : start + batch_size]
            embeddings.extend(embedder.embed(batch))
            if progress:
                progress("embedding", min(start + batch_size, total), total)
        metadatas = []
        for c in children:
            meta: dict = {
                "doc_id": c.doc_id,
                "doc_name": doc_name,
                "section": c.section,
                "kind": c.kind,
                "folder": folder,
            }
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
            store[p.parent_id] = {
                "doc_id": p.doc_id,
                "section": p.section,
                "page_start": p.page_start,
                "text": p.text,
            }
        _save_parents(settings, store)
        get_bm25(settings).update({c.chunk_id: f"{c.section}\n{c.text}" for c in children})


def query(
    settings,
    query_text: str,
    top_k: int,
    folder: str | None = None,
    folders: list[str] | None = None,
) -> list[dict]:
    """返回 [{"sim", "text", "doc_id", ...}]；相似度 = 1 - 余弦距离。

    folder/folders 非空时只召回这些目录子树（含自身）的块。
    """
    from app.indexing.folders import scope_paths

    emb = query_embedding(settings, query_text)
    paths = scope_paths(settings, folder, folders)
    where = None
    if len(paths) == 1:
        where = {"folder": paths[0]}
    elif paths:
        where = {"folder": {"$in": paths}}
    res = _collection(settings).query(
        query_embeddings=[emb],
        n_results=top_k,
        where=where,
        include=["documents", "metadatas", "distances"],
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
                "folder": meta.get("folder") or DEFAULT_FOLDER,
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
                "folder": meta.get("folder") or DEFAULT_FOLDER,
            }
        )
    return hits


def folders_of(settings, chunk_ids: list[str]) -> dict[str, str]:
    """批量取块的目录（BM25 路按目录过滤时使用）。"""
    if not chunk_ids:
        return {}
    res = _collection(settings).get(ids=list(chunk_ids), include=["metadatas"])
    return {
        cid: ((meta or {}).get("folder") or DEFAULT_FOLDER)
        for cid, meta in zip(res["ids"], res["metadatas"])
    }


def vectors_for(settings, chunk_ids: list[str]) -> dict[str, list[float]]:
    """按 chunk_id 批量读取已存向量，供上下文去重复用，避免重复推理。"""
    ids = list(dict.fromkeys(cid for cid in chunk_ids if cid))
    if not ids:
        return {}
    res = _collection(settings).get(ids=ids, include=["embeddings"])
    embeddings = res.get("embeddings")
    if embeddings is None:
        return {}
    return {
        cid: [float(value) for value in vector]
        for cid, vector in zip(res["ids"], embeddings)
        if vector is not None
    }


def doc_profile(settings, doc_id: str, max_chunks: int = 5) -> dict:
    """取文档摘要文本与平均向量，供 AI 分类使用。"""
    res = _collection(settings).get(
        where={"doc_id": doc_id}, include=["documents", "metadatas", "embeddings"]
    )
    rows = sorted(
        zip(
            res.get("ids") or [],
            res.get("documents") or [],
            res.get("metadatas") or [],
            res.get("embeddings") if res.get("embeddings") is not None else [],
        ),
        key=lambda row: row[0],
    )
    texts = [row[1] for row in rows[:max_chunks]]
    sections = [(row[2] or {}).get("section", "") for row in rows[:max_chunks]]
    vectors = [row[3] for row in rows if row[3] is not None]
    vector = None
    if vectors:
        size = len(vectors[0])
        vector = [sum(vec[i] for vec in vectors) / len(vectors) for i in range(size)]
    return {"texts": texts, "sections": sections, "vector": vector}


def folder_neighbors(
    settings, vector: list[float], exclude_doc_id: str | None = None, top_n: int = 60
) -> list[dict]:
    """按向量召回相似块，返回目录维度的相似度与章节证据。"""
    res = _collection(settings).query(
        query_embeddings=[vector],
        n_results=top_n,
        include=["metadatas", "distances"],
    )
    out: list[dict] = []
    for meta, dist in zip(res["metadatas"][0], res["distances"][0]):
        meta = dict(meta or {})
        if exclude_doc_id and meta.get("doc_id") == exclude_doc_id:
            continue
        out.append(
            {
                "folder": meta.get("folder") or DEFAULT_FOLDER,
                "doc_id": meta.get("doc_id", ""),
                "section": meta.get("section", ""),
                "sim": 1.0 - float(dist),
            }
        )
    return out


def update_doc_folder(settings, doc_id: str, folder: str) -> int:
    """只改元数据，不重新嵌入；返回更新的块数。"""
    with write_guard(settings):
        collection = _collection(settings)
        res = collection.get(where={"doc_id": doc_id}, include=["metadatas"])
        ids = res.get("ids") or []
        if not ids:
            return 0
        metadatas = []
        for meta in res.get("metadatas") or []:
            item = dict(meta or {})
            item["folder"] = folder
            metadatas.append(item)
        collection.update(ids=ids, metadatas=metadatas)
        return len(ids)


def update_doc_name(settings, doc_id: str, name: str) -> int:
    """修正展示名（去掉上传临时前缀），只改元数据。"""
    with write_guard(settings):
        collection = _collection(settings)
        res = collection.get(where={"doc_id": doc_id}, include=["metadatas"])
        ids = res.get("ids") or []
        if not ids:
            return 0
        metadatas = []
        for meta in res.get("metadatas") or []:
            item = dict(meta or {})
            item["doc_name"] = name
            metadatas.append(item)
        collection.update(ids=ids, metadatas=metadatas)
        return len(ids)


def delete_doc(settings, doc_id: str) -> None:
    from app.indexing.bm25_index import get_bm25

    with write_guard(settings):
        _collection(settings).delete(where={"doc_id": doc_id})
        try:
            parents = {
                pid: p for pid, p in _load_parents(settings).items() if p["doc_id"] != doc_id
            }
            _save_parents(settings, parents)
        finally:
            # 父块写失败也要保证关键词侧车不残留
            get_bm25(settings).remove_doc(doc_id)


def is_empty(settings) -> bool:
    return _collection(settings).count() == 0
