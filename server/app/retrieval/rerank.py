"""重排钩子：默认恒等（不启用）；提供 cross-encoder 通道，装有余下依赖时可用。

启用方式：INDOCQA_USE_RERANK=true 且环境已安装 sentence-transformers。
"""

import logging

logger = logging.getLogger("indocqa.rerank")

_MODEL = None


def rerank_available() -> bool:
    try:
        import sentence_transformers  # noqa: F401

        return True
    except ImportError:
        return False


def rerank(query: str, hits: list, top_k: int) -> list:
    """可用且开启时按 cross-encoder 分数重排；否则原序截断。"""
    from app.core.config import get_settings

    if not hits:
        return hits
    if not (get_settings().use_rerank and rerank_available()):
        return hits[:top_k]
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import CrossEncoder

        _MODEL = CrossEncoder("BAAI/bge-reranker-v2-m3", max_length=512)
    scores = _MODEL.predict([(query, h.text[:1200]) for h in hits])
    ranked = sorted(zip(hits, scores), key=lambda p: p[1], reverse=True)
    logger.info("rerank 应用于 %d 条候选", len(hits))
    return [h for h, _ in ranked[:top_k]]
