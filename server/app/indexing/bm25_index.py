"""BM25 侧车索引：与 Chroma 子块对齐的关键词检索（jieba 分词），支持增量更新与删除。"""

import json
import pathlib

import jieba
from rank_bm25 import BM25Okapi

_TOKEN_CACHE: dict[str, list[str]] = {}


def tokenize(text: str) -> list[str]:
    key = text[:64]
    if key in _TOKEN_CACHE:
        return _TOKEN_CACHE[key]
    tokens = [t for t in jieba.lcut_for_search(text.lower()) if t.strip() and t not in {" ", "\n"}]
    if len(_TOKEN_CACHE) > 5000:
        _TOKEN_CACHE.clear()
    _TOKEN_CACHE[key] = tokens
    return tokens


class BM25Index:
    """chunk_id → 文本 的侧车存储 + 惰性构建的 BM25 实例（文档集变更时重建）。"""

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.docs: dict[str, str] = {}
        if path.exists():
            self.docs = json.loads(path.read_text(encoding="utf-8"))
        self._instance: BM25Okapi | None = None
        self._ids: list[str] = []

    def update(self, items: dict[str, str]) -> None:
        self.docs.update(items)
        self._instance = None
        self._flush()

    def remove_doc(self, doc_id: str) -> None:
        self.docs = {k: v for k, v in self.docs.items() if not k.startswith(doc_id + "-")}
        self._instance = None
        self._flush()

    def search(self, query: str, top_k: int) -> list[tuple[str, float]]:
        self._ensure()
        if not self._ids:
            return []
        scores = self._instance.get_scores(tokenize(query))
        ranked = sorted(zip(self._ids, scores), key=lambda x: x[1], reverse=True)[:top_k]
        return [(cid, float(sc)) for cid, sc in ranked if sc > 0]

    def _ensure(self) -> None:
        if self._instance is not None:
            return
        self._ids = list(self.docs.keys())
        corpus = [tokenize(self.docs[cid]) for cid in self._ids]
        corpus = [c if c else ["空"] for c in corpus]
        self._instance = BM25Okapi(corpus)

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.docs, ensure_ascii=False), encoding="utf-8")


def get_bm25(settings) -> BM25Index:
    return BM25Index(pathlib.Path(settings.chroma_dir) / "bm25_docs.json")
