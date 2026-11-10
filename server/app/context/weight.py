"""多信号权重评分：相关性、意图、实体、权威度、画像、任务与新颖度。"""

import json
import re

from app.context.schema import ContextUnit, PersonaFact, TaskState

DEFAULT_WEIGHTS = {
    "rel": 0.38,
    "intent": 0.18,
    "entity": 0.14,
    "authority": 0.08,
    "persona": 0.07,
    "task": 0.07,
    "novelty": 0.08,
}

_ENTITY = re.compile(
    r"[A-Za-z][A-Za-z0-9.\-]{1,14}|\d+(?:\.\d+)?\s*(?:MPa|kPa|bar|℃|°C|mm|cm|m|kg|h|小时|分钟|天|V|A|kW|rpm|%)?"
)
_TOKEN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9.\-]{1,14}|\d+(?:\.\d+)?")
_STOP = {"什么", "怎么", "如何", "多少", "这个", "那个", "我们", "你们", "请问", "是否", "可以"}


def _coefficients(settings) -> dict:
    raw = getattr(settings, "context_weights", "") or ""
    if not raw:
        return dict(DEFAULT_WEIGHTS)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return dict(DEFAULT_WEIGHTS)
    if not isinstance(data, dict):
        return dict(DEFAULT_WEIGHTS)
    return {key: float(data.get(key, value)) for key, value in DEFAULT_WEIGHTS.items()}


def extract_entities(text: str, extra: list[str] | None = None) -> list[str]:
    found = [m.strip() for m in _ENTITY.findall(text or "") if m.strip()]
    merged = list(extra or []) + found
    seen, out = set(), []
    for item in merged:
        key = item.lower()
        if len(key) < 2 or key in _STOP or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out[:20]


def _tokens(text: str) -> set[str]:
    return {m.lower() for m in _TOKEN.findall(text or "") if len(m) >= 2 and m not in _STOP}


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _min_max(values: list[float]) -> list[float]:
    if not values:
        return []
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return [0.5 for _ in values]
    return [(value - low) / (high - low) for value in values]


def _authority(unit: ContextUnit) -> float:
    if unit.layer == "history":
        return 0.5
    if unit.layer in {"persona", "task"}:
        return 1.0
    name = f"{unit.ref.doc_name or ''}{unit.ref.section or ''}"
    if "标准" in name or "规范" in name:
        return 0.9
    if "手册" in name or "规程" in name or "说明书" in name:
        return 1.0
    if "论文" in name or "白皮书" in name:
        return 0.8
    return 0.7


def _intent_fit(unit: ContextUnit, intent: str) -> float:
    if unit.layer != "evidence":
        return 0.5
    text = f"{unit.ref.section or ''}{unit.text}"
    if intent == "param":
        return 1.0 if unit.meta.get("kind") == "table" else 0.3
    if intent in {"procedure", "fault"}:
        return (
            1.0 if any(k in text for k in ("步骤", "处置", "处理", "更换", "检查", "排查")) else 0.4
        )
    if intent == "term":
        return 1.0 if any(k in text for k in ("是指", "定义", "全称", "含义", "即")) else 0.5
    if intent == "multihop":
        return 0.8
    return 0.5


def _persona_fit(unit: ContextUnit, persona: list[PersonaFact]) -> float:
    if not persona:
        return 0.0
    text = unit.text.lower()
    tokens = set()
    for fact in persona:
        tokens |= _tokens(str(fact.value))
    if not tokens:
        return 0.0
    return _clamp(sum(1 for token in tokens if token in text) / len(tokens))


def _task_fit(unit: ContextUnit, task: TaskState | None) -> float:
    if task is None:
        return 0.0
    text = unit.text.lower()
    tokens = set()
    tokens |= _tokens(task.topic)
    for entity in task.entities:
        tokens |= _tokens(entity)
    if not tokens:
        return 0.0
    return _clamp(sum(1 for token in tokens if token in text) / len(tokens))


def score_units(
    units: list[ContextUnit],
    settings,
    intent: str = "general",
    entities: list[str] | None = None,
    persona: list[PersonaFact] | None = None,
    task: TaskState | None = None,
) -> list[ContextUnit]:
    if not units:
        return []
    weights = _coefficients(settings)
    entities = entities or []
    persona = persona or []
    evidence = [u for u in units if u.layer == "evidence"]
    evidence_ids = {u.unit_id for u in evidence}
    rels = [
        _clamp(
            float(u.meta.get("sim") if u.meta.get("sim") is not None else u.meta.get("rel", 0.0))
        )
        for u in evidence
    ]
    rel_norm = dict(zip([u.unit_id for u in evidence], _min_max(rels)))

    history = [u for u in units if u.layer == "history"]
    history_order = {u.unit_id: index for index, u in enumerate(history)}

    for unit in units:
        if unit.unit_id in evidence_ids:
            rel = rel_norm.get(unit.unit_id, 0.0)
        elif unit.layer == "history":
            index = history_order.get(unit.unit_id, 0)
            rel = 1.0 / (1.0 + 0.15 * index)
        elif unit.layer == "persona":
            rel = float(unit.meta.get("confidence", 0.6))
        else:
            rel = 1.0
        entity_hit = 0.0
        if entities:
            text = unit.text.lower()
            hits = sum(1 for item in entities if item.lower() in text)
            entity_hit = _clamp(hits / len(entities))
        unit.meta["entity_hit"] = entity_hit
        base = (
            weights["rel"] * rel
            + weights["intent"] * _intent_fit(unit, intent)
            + weights["entity"] * entity_hit
            + weights["authority"] * _authority(unit)
            + weights["persona"] * _persona_fit(unit, persona)
            + weights["task"] * _task_fit(unit, task)
        )
        unit.meta["base_weight"] = _clamp(base)
        unit.weight = _clamp(base)

    ordered = sorted(evidence, key=lambda u: u.weight, reverse=True)
    seen: list[set[str]] = []
    for unit in ordered:
        tokens = _tokens(unit.text)
        overlap = 0.0
        for previous in seen:
            union = tokens | previous
            if union:
                overlap = max(overlap, len(tokens & previous) / len(union))
        unit.weight = _clamp(unit.meta["base_weight"] + weights["novelty"] * (1.0 - overlap))
        unit.meta["novelty"] = round(1.0 - overlap, 4)
        seen.append(tokens)
    return units
