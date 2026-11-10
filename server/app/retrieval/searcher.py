"""混合检索：向量召回 + BM25 关键词召回，RRF 融合排序。

RRF（Reciprocal Rank Fusion）只依赖排名而不依赖分数尺度，
对语义（向量）与精确匹配（BM25，故障码/型号/术语缩写）两类信号无偏融合。
"""

import re
from dataclasses import dataclass

from app.core.config import get_settings
from app.indexing import store
from app.indexing.bm25_index import get_bm25

RRF_K = 60
EXPAND = 3  # 两路召回各取 top_k * EXPAND 再融合
CODE_BOOST = 0.02  # 故障码/型号精确命中时叠加到 RRF 分数
# 单路高分补偿：向量 top3 但 BM25 未召回时（跨语种/中文问英文文档）追加的 RRF 分
SINGLE_ROUTE_BOOST = 0.5
CROSS_LINGUAL_BOOST = 1.0
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _cross_lingual(query: str, text: str) -> bool:
    """中文问句命中英文块时，BM25 天然失效，给向量高分块一整票补偿。"""
    return bool(_CJK.search(query)) and not bool(_CJK.search(text[:200]))


_CODE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,4}[-_]?\d{1,4}(?![A-Za-z0-9])")


def _code_bonus(query: str, text: str) -> float:
    """故障码/型号（E-05 / E05 / P4）精确命中加权，弥补 BM25 被高频词稀释。"""
    wanted = {c.upper().replace("-", "").replace("_", "") for c in _CODE.findall(query)}
    if not wanted:
        return 0.0
    found = {c.upper().replace("-", "").replace("_", "") for c in _CODE.findall(text)}
    return CODE_BOOST if wanted & found else 0.0


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
    folder: str = ""


def search(
    query: str,
    top_k: int | None = None,
    min_sim: float | None = None,
    settings=None,
    query_lex: str | None = None,
    strategy: str = "hybrid",
    folder: str | None = None,
) -> list[Hit]:
    """混合检索入口。

    - query：语义路（向量）查询；query_lex：词法路（BM25）查询，术语扩展词只进词法路，
      避免扩展词稀释向量语义（消融实验发现稀释会降低召回）。
    - strategy：hybrid 两路融合；keyword 仅 BM25（故障码等精确匹配）；vector 仅向量；
      none 直接返回空（寒暄等无需检索）。
    - min_sim 仅作用于向量命中那一路。
    - folder：目录子树过滤（含自身），空表示全库。
    """
    if strategy == "none":
        return []
    settings = settings or get_settings()
    top_k = top_k or settings.top_k_default
    lex = query_lex or query

    allowed_paths: set[str] | None = None
    if folder:
        from app.indexing.folders import descendants
        from app.indexing.manifest import Manifest

        paths = descendants(folder, Manifest(Manifest.path_for(settings)).folders())
        allowed_paths = set(paths) or {folder}

    by_id: dict[str, dict] = {}
    ranks: dict[str, float] = {}
    vec_rank: dict[str, int] = {}
    bm25_ids: set[str] = set()
    if strategy != "keyword":
        if min_sim is None:
            min_sim = settings.sim_threshold
        for rank, h in enumerate(store.query(settings, query, top_k * EXPAND, folder=folder)):
            if h["sim"] < min_sim:
                continue
            vec_rank[h["chunk_id"]] = rank
            by_id[h["chunk_id"]] = h
            ranks[h["chunk_id"]] = ranks.get(h["chunk_id"], 0.0) + 1.0 / (RRF_K + rank + 1)
    if strategy != "vector":
        bm25 = get_bm25(settings)
        # 原始查询的 BM25 命中按正常名次融合
        raw_hits = bm25.search(query, top_k * EXPAND)
        if allowed_paths is not None:
            mapping = store.folders_of(settings, [cid for cid, _ in raw_hits])
            raw_hits = [
                (cid, score) for cid, score in raw_hits if mapping.get(cid) in allowed_paths
            ]
        for rank, (cid, _score) in enumerate(raw_hits):
            bm25_ids.add(cid)
            ranks[cid] = ranks.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
            if cid not in by_id:
                by_id[cid] = None
        # 扩展查询的命中整体名次后移（降权），只补位不挤占原始命中
        if lex != query:
            offset = top_k * EXPAND
            lex_hits = bm25.search(lex, top_k * EXPAND)
            if allowed_paths is not None:
                mapping = store.folders_of(settings, [cid for cid, _ in lex_hits])
                lex_hits = [
                    (cid, score) for cid, score in lex_hits if mapping.get(cid) in allowed_paths
                ]
            for rank, (cid, _score) in enumerate(lex_hits):
                # 扩展词命中只降权补位，不视为“原始 BM25 已召回”，
                # 否则跨语种高分块会因扩展词偶然命中而失去单路补偿
                ranks[cid] = ranks.get(cid, 0.0) + 1.0 / (RRF_K + offset + rank + 1)
                if cid not in by_id:
                    by_id[cid] = None

    missing = [cid for cid, h in by_id.items() if h is None]
    for h in store.get_by_ids(settings, missing):
        by_id[h["chunk_id"]] = h

    hits: list[Hit] = []
    candidates: list[tuple[str, float]] = []
    for cid, rrf in ranks.items():
        raw = by_id.get(cid)
        if raw is None:
            continue
        score = rrf + _code_bonus(query, raw["text"])
        if cid not in bm25_ids and vec_rank.get(cid, 99) < 3:
            # 单路补偿：只补向量高分且 BM25 完全未召回的块，避免改变双路命中的排序
            factor = (
                CROSS_LINGUAL_BOOST if _cross_lingual(query, raw["text"]) else SINGLE_ROUTE_BOOST
            )
            score += factor / (RRF_K + vec_rank[cid] + 1)
        candidates.append((cid, score))
    for cid, rrf in sorted(candidates, key=lambda kv: kv[1], reverse=True)[:top_k]:
        raw = by_id[cid]
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
                folder=raw.get("folder", ""),
            )
        )
    hits.sort(key=lambda h: h.rrf, reverse=True)
    return hits
