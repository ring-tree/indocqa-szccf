"""混合检索：向量召回 + BM25 关键词召回，RRF 融合排序。

RRF（Reciprocal Rank Fusion）只依赖排名而不依赖分数尺度，
对语义（向量）与精确匹配（BM25，故障码/型号/术语缩写）两类信号无偏融合。
"""

from dataclasses import dataclass

from app.core.config import get_settings
from app.indexing import store
from app.indexing.bm25_index import get_bm25

RRF_K = 60
EXPAND = 3  # 两路召回各取 top_k * EXPAND 再融合


@dataclass
class Hit:
    chunk_id: str
    rrf: float
    sim: float | None
    text: str
    doc_id: str
    doc_name: str
    section: str
    page: int | None
    kind: str
    table_no: str | None
    parent_text: str


def search(
    query: str,
    top_k: int | None = None,
    min_sim: float | None = None,
    settings=None,
    query_lex: str | None = None,
    strategy: str = "hybrid",
) -> list[Hit]:
    """混合检索入口。

    - query：语义路（向量）查询；query_lex：词法路（BM25）查询，术语扩展词只进词法路，
      避免扩展词稀释向量语义（消融实验发现稀释会降低召回）。
    - strategy：hybrid 两路融合；keyword 仅 BM25（故障码等精确匹配）；vector 仅向量。
    - min_sim 仅作用于向量命中那一路。
    """
    settings = settings or get_settings()
    top_k = top_k or settings.top_k_default
    lex = query_lex or query

    by_id: dict[str, dict] = {}
    ranks: dict[str, float] = {}
    if strategy != "keyword":
        if min_sim is None:
            min_sim = settings.sim_threshold
        for rank, h in enumerate(store.query(settings, query, top_k * EXPAND)):
            if h["sim"] < min_sim:
                continue
            by_id[h["chunk_id"]] = h
            ranks[h["chunk_id"]] = ranks.get(h["chunk_id"], 0.0) + 1.0 / (RRF_K + rank + 1)
    if strategy != "vector":
        bm25 = get_bm25(settings)
        # 原始查询的 BM25 命中按正常名次融合
        for rank, (cid, _score) in enumerate(bm25.search(query, top_k * EXPAND)):
            ranks[cid] = ranks.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
            if cid not in by_id:
                by_id[cid] = None
        # 扩展查询的命中整体名次后移（降权），只补位不挤占原始命中
        if lex != query:
            offset = top_k * EXPAND
            for rank, (cid, _score) in enumerate(bm25.search(lex, top_k * EXPAND)):
                ranks[cid] = ranks.get(cid, 0.0) + 1.0 / (RRF_K + offset + rank + 1)
                if cid not in by_id:
                    by_id[cid] = None

    missing = [cid for cid, h in by_id.items() if h is None]
    for h in store.get_by_ids(settings, missing):
        by_id[h["chunk_id"]] = h

    hits: list[Hit] = []
    for cid, rrf in sorted(ranks.items(), key=lambda kv: kv[1], reverse=True)[:top_k]:
        raw = by_id.get(cid)
        if raw is None:
            continue
        parent = store.get_parent_text(settings, raw["doc_id"], raw["section"])
        hits.append(
            Hit(
                chunk_id=cid,
                rrf=round(rrf, 5),
                sim=raw["sim"],
                text=raw["text"],
                doc_id=raw["doc_id"],
                doc_name=raw["doc_name"],
                section=raw["section"],
                page=raw.get("page"),
                kind=raw.get("kind", "text"),
                table_no=raw.get("table_no"),
                parent_text=parent,
            )
        )
    return hits
