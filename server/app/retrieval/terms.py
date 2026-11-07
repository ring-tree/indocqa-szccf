"""术语归一：同义簇构建（种子 + 语料抽取）、查询扩展、索引侧打标。

簇文件 data/terms/terms.json 由构建命令生成并随库提交；种子见 seed.json。
"""

import json
import pathlib
import re
from collections import defaultdict

from app.core.config import get_settings

# 中文（英文/缩写） 与 缩写（中文全称） 两种定义形态
_PAIR_ZH = re.compile(r"([\u4e00-\u9fff]{2,12})\s*[（(]([A-Za-z][A-Za-z0-9 .\-/]{2,40})[)）]")
_PAIR_ABBR = re.compile(r"\b([A-Z][A-Za-z0-9.]{1,14})\s*[（(]([^（）()]{2,40})[)）]")
_PAIR_EN = re.compile(r"\b([A-Za-z][A-Za-z0-9\-]{2,30})\s*\(([^()]{2,40}?)\)")
_MAX_EXPANSION = 6


def _seed_path(settings) -> pathlib.Path:
    return pathlib.Path(settings.terms_file).parent / "seed.json"


def load_clusters(settings=None) -> list[list[str]]:
    settings = settings or get_settings()
    path = pathlib.Path(settings.terms_file)
    if not path.exists():
        return build_clusters(settings)
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("clusters", [])


def build_clusters(settings=None) -> list[list[str]]:
    """种子簇 ∪ 语料抽取簇（并查集合并），写回 terms.json 并给索引打同义标签。"""
    settings = settings or get_settings()
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        if a != b:
            parent[find(a)] = find(b)

    seed_file = _seed_path(settings)
    protected: set[str] = set()
    if seed_file.exists():
        for cluster in json.loads(seed_file.read_text(encoding="utf-8"))["clusters"]:
            protected.update(m.lower() for m in cluster)
            for m in cluster[1:]:
                union(cluster[0], m)

    for a, b in _extract_pairs(settings):
        union(a.strip(), b.strip())

    # 频次过滤：抽取出的成员在语料中仅出现一次的多为噪声（乱序文本、偶然共现）
    from app.indexing.bm25_index import get_bm25

    corpus_text = " ".join(get_bm25(settings).docs.values())
    groups: dict[str, list[str]] = defaultdict(list)
    for name in parent:
        if len(name) >= 3 and name.lower() not in protected and corpus_text.count(name) < 2:
            continue
        groups[find(name)].append(name)
    clusters = [sorted(members, key=len) for members in groups.values() if len(members) >= 2]
    clusters.sort(key=lambda c: c[0].lower())

    out = pathlib.Path(settings.terms_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"builtAt": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"), "clusters": clusters},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    apply_terms_to_index(settings, clusters)
    return clusters


def _plausible_pair(a: str, b: str) -> bool:
    """缩写侧需以大写字母为主（真缩写），排除编号、普通单词等噪声形态。"""
    a, b = a.strip(), b.strip()
    if not a or not b or a.isdigit() or b.isdigit():
        return False
    if re.fullmatch(r"[A-Za-z]?\d+[A-Za-z]?", a):  # A1 / T1 / 800 之类编号
        return False
    if re.search(r"[\u4e00-\u9fff]", a):
        return 2 <= len(a) <= 12 and len(b) >= 2
    letters = [c for c in a if c.isalpha()]
    if not letters:
        return False
    upper = sum(1 for c in letters if c.isupper()) / len(letters)
    return upper >= 0.6 and 2 <= len(a) <= 15 and 2 <= len(b) <= 60


def _extract_pairs(settings) -> list[tuple[str, str]]:
    """从 BM25 侧车（含章节标题的块文本）抽取“缩写(全称)”形态的同义对。"""
    from app.indexing.bm25_index import get_bm25

    texts = get_bm25(settings).docs.values()
    pairs: list[tuple[str, str]] = []
    for text in texts:
        for a, b in _PAIR_ZH.findall(text):
            pairs.append((a, b))
        for a, b in _PAIR_ABBR.findall(text):
            pairs.append((a, b))
        for a, b in _PAIR_EN.findall(text):
            if a.isupper() or b.isupper():  # 只保留缩写形态，过滤普通从句
                pairs.append((a, b))
    seen = set()
    uniq = []
    for p in pairs:
        key = (p[0].lower(), p[1].lower())
        if key not in seen and _plausible_pair(*p):
            seen.add(key)
            uniq.append(p)
    return uniq


def expand_query(question: str, clusters: list[list[str]]) -> tuple[str, list[list[str]]]:
    """查询扩展：命中簇的其余成员追加到查询（限流，防止查询过长稀释语义）。"""
    added: list[str] = []
    used: list[list[str]] = []
    low = question.lower()
    for cluster in clusters:
        matched = [m for m in cluster if len(m) >= 2 and m.lower() in low]
        if not matched:
            continue
        rest = [m for m in cluster if m not in matched and len(m) >= 2]
        if rest and len(added) < _MAX_EXPANSION:
            added.append(rest[0])
            if len(rest) > 1 and len(added) < _MAX_EXPANSION:
                added.append(rest[1])
            used.append(cluster)
    if not added:
        return question, []
    return f"{question} {' '.join(added)}", used


def apply_terms_to_index(settings, clusters: list[list[str]]) -> None:
    """索引侧打标：把命中的同义成员附加到 BM25 侧车文本，实现双向归一。"""
    from app.indexing import store

    collection = store._collection(settings)
    res = collection.get(include=["documents", "metadatas"])
    updates: dict[str, str] = {}
    for cid, doc, meta in zip(res["ids"], res["documents"], res["metadatas"]):
        low = doc.lower()
        extra = set()
        for cluster in clusters:
            if any(len(m) >= 3 and m.lower() in low for m in cluster):
                extra.update(m for m in cluster if len(m) >= 2)
        if extra:
            base = f"{(meta or {}).get('section', '')}\n{doc}"
            updates[cid] = f"{base}\n同义: {' '.join(sorted(extra)[:12])}"
    if updates:
        from app.indexing.bm25_index import get_bm25

        get_bm25(settings).update(updates)


def main() -> int:
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    settings = get_settings()
    clusters = build_clusters(settings)
    print(f"术语簇 {len(clusters)} 个，已写入 {settings.terms_file}")
    for c in clusters[:8]:
        print("  " + " / ".join(c[:4]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
