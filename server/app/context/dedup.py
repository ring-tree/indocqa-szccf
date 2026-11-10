"""三级去重：精确哈希 → SimHash 近重复 → 复用向量做语义重复。

语义重复增加“数字/型号互补性保护”，避免把同模板不同参数的表格误删。
"""

import hashlib
import re
import unicodedata
from collections import defaultdict

from app.context.schema import ContextUnit, DroppedUnit

_PUNCT = re.compile(r"[^\w\u3400-\u4dbf\u4e00-\u9fff]+", re.UNICODE)
_SIGNATURE = re.compile(r"[A-Za-z]+[-\d]*|\d+(?:\.\d+)?")

DEFAULT_HAMMING = 3
DEFAULT_THRESHOLD = 0.92
DEFAULT_JACCARD = 0.82


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "").lower()
    value = value.replace("\u200b", "").replace("\ufeff", "")
    return _PUNCT.sub("", value)


def _digest(text: str) -> int:
    return int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "big")


def _shingles(text: str) -> list[str]:
    norm = normalize_text(text)
    if not norm:
        return []
    if len(norm) <= 3:
        return [norm]
    return [norm[i : i + 3] for i in range(len(norm) - 2)]


def simhash64(text: str) -> int:
    shingles = _shingles(text)
    if not shingles:
        return 0
    vector = [0] * 64
    for shingle in shingles:
        value = _digest(shingle)
        for bit in range(64):
            vector[bit] += 1 if (value >> bit) & 1 else -1
    out = 0
    for bit in range(64):
        if vector[bit] > 0:
            out |= 1 << bit
    return out


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _signature(text: str) -> set[str]:
    return {m.lower() for m in _SIGNATURE.findall(text or "")}


def _complementary(a: str, b: str) -> bool:
    """数字/型号差异显著时视为互补信息，保留两者。"""
    left, right = _signature(a), _signature(b)
    if not left or not right:
        return False
    union = left | right
    if not union:
        return False
    return len(left & right) / len(union) < 0.5


def _pre_score(unit: ContextUnit, max_rrf: float) -> float:
    sim = unit.meta.get("sim")
    rrf = float(unit.meta.get("rrf") or 0.0)
    if sim is not None:
        return float(sim)
    return rrf / max_rrf if max_rrf > 0 else 0.0


def _better(a: ContextUnit, b: ContextUnit, max_rrf: float) -> ContextUnit:
    """代表元：相关性高者优先，其次表格，其次更长文本，最后按 ID 稳定。"""
    key_a = (_pre_score(a, max_rrf), a.meta.get("kind") == "table", len(a.text), a.unit_id)
    key_b = (_pre_score(b, max_rrf), b.meta.get("kind") == "table", len(b.text), b.unit_id)
    return a if key_a >= key_b else b


def _drop(unit: ContextUnit, reason: str, duplicate_of: str) -> DroppedUnit:
    return DroppedUnit(
        unit_id=unit.unit_id,
        layer=unit.layer,
        reason=reason,
        tokens=unit.tokens,
        excerpt=f"{duplicate_of}|{unit.text[:60].replace(chr(10), ' ')}",
    )


def _shingle_set(text: str) -> set[str]:
    return set(_shingles(text))


def _near_pairs_fast(
    ids: list[str],
    hashes: dict[str, int],
    shingles: dict[str, set[str]],
    signatures: dict[str, set[str]],
    limit: int,
    threshold: float,
) -> list[tuple[str, str]]:
    pairs = []
    for i, left in enumerate(ids):
        for right in ids[i + 1 :]:
            # 数字/型号集合不同（如不同设备的同模板段落）视为互补信息，不做近重复合并
            if signatures[left] and signatures[right] and signatures[left] != signatures[right]:
                continue
            if hamming(hashes[left], hashes[right]) <= limit:
                pairs.append((left, right))
                continue
            a, b = shingles[left], shingles[right]
            if a and b and len(a & b) / len(a | b) >= threshold:
                pairs.append((left, right))
    return pairs


def deduplicate(
    units: list[ContextUnit],
    vectors: dict[str, list[float]] | None = None,
    settings=None,
    embedder=None,
) -> tuple[list[ContextUnit], list[DroppedUnit]]:
    if len(units) < 2:
        return list(units), []
    max_rrf = max((float(u.meta.get("rrf") or 0.0) for u in units), default=0.0)
    kept, dropped = _exact(units, max_rrf)
    jaccard = float(getattr(settings, "context_dedup_jaccard", DEFAULT_JACCARD))
    kept, near_dropped = _near(
        kept,
        max_rrf,
        int(getattr(settings, "context_dedup_hamming", DEFAULT_HAMMING)),
        jaccard,
    )
    dropped.extend(near_dropped)
    threshold = float(getattr(settings, "context_dedup_semantic", DEFAULT_THRESHOLD))
    enabled = bool(getattr(settings, "context_dedup_semantic_enabled", True))
    if enabled:
        kept, semantic_dropped = _semantic(kept, vectors or {}, threshold, max_rrf, embedder)
        dropped.extend(semantic_dropped)
    return kept, dropped


def _exact(units: list[ContextUnit], max_rrf: float) -> tuple[list[ContextUnit], list[DroppedUnit]]:
    groups: dict[str, list[ContextUnit]] = defaultdict(list)
    for unit in units:
        groups[normalize_text(unit.text) or unit.unit_id].append(unit)
    kept, dropped = [], []
    for group in groups.values():
        if len(group) == 1:
            kept.append(group[0])
            continue
        best = group[0]
        for candidate in group[1:]:
            best = _better(best, candidate, max_rrf)
        kept.append(best)
        for unit in group:
            if unit is not best:
                dropped.append(_drop(unit, "duplicate:exact", best.unit_id))
    return kept, dropped


def _lsh_buckets(hashes: dict[str, int]) -> dict[tuple[int, int], list[str]]:
    buckets: dict[tuple[int, int], list[str]] = defaultdict(list)
    for unit_id, value in hashes.items():
        for band in range(4):
            buckets[(band, (value >> (band * 16)) & 0xFFFF)].append(unit_id)
    return buckets


def _near(
    units: list[ContextUnit], max_rrf: float, hamming_limit: int, jaccard: float
) -> tuple[list[ContextUnit], list[DroppedUnit]]:
    if len(units) < 2:
        return list(units), []
    hashes = {u.unit_id: simhash64(u.text) for u in units}
    shingles = {u.unit_id: _shingle_set(u.text) for u in units}
    signatures = {u.unit_id: _signature(u.text) for u in units}
    by_id = {u.unit_id: u for u in units}
    pairs: list[tuple[str, str]] = []
    if len(units) <= 128:
        pairs = _near_pairs_fast(list(by_id), hashes, shingles, signatures, hamming_limit, jaccard)
    else:
        for bucket in _lsh_buckets(hashes).values():
            pairs.extend(
                _near_pairs_fast(bucket, hashes, shingles, signatures, hamming_limit, jaccard)
            )
    if not pairs:
        return list(units), []
    return _merge_pairs(units, pairs, max_rrf, "duplicate:near")


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _semantic(
    units: list[ContextUnit],
    vectors: dict[str, list[float]],
    threshold: float,
    max_rrf: float,
    embedder=None,
) -> tuple[list[ContextUnit], list[DroppedUnit]]:
    evidence = [u for u in units if u.layer == "evidence"]
    if len(evidence) < 2:
        return list(units), []
    ids = [u.meta.get("chunk_id") or "" for u in evidence]
    missing = [u for u, cid in zip(evidence, ids) if cid and cid not in vectors]
    if missing and embedder is not None:
        texts = [u.text for u in missing[:16]]
        try:
            embedded = embedder.query_embed(texts)
            for unit, vector in zip(missing, embedded):
                vectors[unit.meta.get("chunk_id") or ""] = list(vector)
        except (OSError, RuntimeError, ValueError):
            embedded = []

    pairs: list[tuple[str, str]] = []
    for i, left in enumerate(evidence):
        left_vec = vectors.get(ids[i])
        if not left_vec:
            continue
        for right in evidence[i + 1 :]:
            right_vec = vectors.get(right.meta.get("chunk_id") or "")
            if not right_vec:
                continue
            if _cosine(left_vec, right_vec) < threshold:
                continue
            if _complementary(left.text, right.text):
                continue
            pairs.append((left.unit_id, right.unit_id))
    if not pairs:
        return list(units), []
    return _merge_pairs(units, pairs, max_rrf, "duplicate:semantic")


def _merge_pairs(
    units: list[ContextUnit], pairs: list[tuple[str, str]], max_rrf: float, reason: str
) -> tuple[list[ContextUnit], list[DroppedUnit]]:
    parent = {u.unit_id: u.unit_id for u in units}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for left, right in pairs:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a
    groups: dict[str, list[ContextUnit]] = defaultdict(list)
    for unit in units:
        groups[find(unit.unit_id)].append(unit)

    kept, dropped = [], []
    for group in groups.values():
        if len(group) == 1:
            kept.append(group[0])
            continue
        best = group[0]
        for candidate in group[1:]:
            best = _better(best, candidate, max_rrf)
        kept.append(best)
        for unit in group:
            if unit is not best:
                dropped.append(_drop(unit, reason, best.unit_id))
    order = {u.unit_id: i for i, u in enumerate(units)}
    kept.sort(key=lambda u: order[u.unit_id])
    return kept, dropped
