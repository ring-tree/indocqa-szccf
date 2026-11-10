"""预算规划：固定区预留、分层配额、按权重贪心选择、末位裁剪与硬断言。"""

import math
import re
from dataclasses import dataclass, replace

from app.context.errors import BudgetInfeasibleError
from app.context.schema import ContextUnit, DroppedUnit

LAYERS = ("evidence", "task", "history", "persona")
TRUNCATABLE = {"evidence", "history", "persona", "task"}
MIN_UNIT_TOKENS = 24
_SENTENCE = re.compile(r"(?<=[。！？!?\n；;])")


@dataclass
class BudgetPlan:
    kept: list[ContextUnit]
    dropped: list[DroppedUnit]
    budget: int
    effective: int
    fixed: int
    layer_tokens: dict
    layer_units: dict


def _ratio(settings, layer: str, default: float) -> float:
    return float(getattr(settings, f"context_quota_{layer}", default))


def _truncate(unit: ContextUnit, max_tokens: int, counter) -> ContextUnit | None:
    if max_tokens < MIN_UNIT_TOKENS:
        return None
    parts = [p for p in _SENTENCE.split(unit.text) if p]
    out = ""
    for part in parts:
        candidate = out + part
        if counter.count_message("", candidate) > max_tokens:
            break
        out = candidate
    if not out:
        ratio = max_tokens / max(1, unit.tokens)
        out = unit.text[: max(8, int(len(unit.text) * ratio * 0.8))]
    text = out.rstrip() + "…"
    tokens = counter.count_message("", text)
    if tokens > max_tokens:
        text = text[: max(8, len(text) - (tokens - max_tokens) * 2)] + "…"
        tokens = counter.count_message("", text)
    return replace(unit, text=text, tokens=tokens, flags=[*unit.flags, "truncated"])


def _drop(unit: ContextUnit, reason: str) -> DroppedUnit:
    return DroppedUnit(
        unit_id=unit.unit_id,
        layer=unit.layer,
        reason=reason,
        tokens=unit.tokens,
        excerpt=unit.text[:60].replace("\n", " "),
    )


def plan_budget(
    units: list[ContextUnit],
    settings,
    counter,
    fixed_tokens: int,
    question_tokens: int,
    message_overhead: int = 8,
) -> BudgetPlan:
    window = int(getattr(settings, "context_window_tokens", 32768))
    safety = float(getattr(settings, "context_safety_ratio", 0.90))
    reserve = int(getattr(settings, "context_output_reserve", 1500))
    effective = max(0, math.floor(window * safety) - reserve)
    fixed = fixed_tokens + question_tokens + message_overhead
    available = effective - fixed
    if available < MIN_UNIT_TOKENS:
        raise BudgetInfeasibleError(
            "固定区已超出上下文预算", detail=f"effective={effective} fixed={fixed}"
        )

    quotas = {
        "evidence": int(available * _ratio(settings, "evidence", 0.60)),
        "history": int(available * _ratio(settings, "history", 0.18)),
        "task": int(available * _ratio(settings, "task", 0.14)),
        "persona": int(available * _ratio(settings, "persona", 0.08)),
    }
    grouped: dict[str, list[ContextUnit]] = {layer: [] for layer in LAYERS}
    for unit in units:
        grouped.setdefault(unit.layer, []).append(unit)

    kept: list[ContextUnit] = []
    deferred: list[ContextUnit] = []
    used = 0
    layer_tokens = {layer: 0 for layer in LAYERS}
    layer_units = {layer: 0 for layer in LAYERS}

    for layer in LAYERS:
        quota = quotas[layer]
        layer_used = 0
        ordered = sorted(grouped.get(layer, []), key=lambda u: u.weight, reverse=True)
        for index, unit in enumerate(ordered):
            if used + unit.tokens > available:
                deferred.extend(ordered[index:])
                break
            if layer_used + unit.tokens <= quota:
                kept.append(unit)
                layer_used += unit.tokens
                used += unit.tokens
            else:
                # 配额已满后不再跳选更小的低权重单元，保持权重前缀
                deferred.extend(ordered[index:])
                break
        layer_tokens[layer] = layer_used
        layer_units[layer] = sum(1 for unit in kept if unit.layer == layer)

    _guarantee_minimums(grouped, kept, available, layer_tokens, layer_units)
    used = sum(unit.tokens for unit in kept)

    # 二次分配：全局按权重降序，优先补高权重单元，必要时末位裁剪，保持保留集合与权重单调
    selected = {unit.unit_id for unit in kept}
    for unit in sorted(deferred, key=lambda u: u.weight, reverse=True):
        if unit.unit_id in selected:
            continue
        room = available - used
        if room < MIN_UNIT_TOKENS:
            break
        if unit.tokens <= room:
            kept.append(unit)
            selected.add(unit.unit_id)
            used += unit.tokens
            layer_tokens[unit.layer] += unit.tokens
            layer_units[unit.layer] += 1
            continue
        if unit.layer in TRUNCATABLE and unit.meta.get("kind") != "table":
            truncated = _truncate(unit, room, counter)
            if truncated is not None:
                kept.append(truncated)
                selected.add(unit.unit_id)
                used += truncated.tokens
                layer_tokens[unit.layer] += truncated.tokens
                layer_units[unit.layer] += 1
                # 末位裁剪后预算基本耗尽，停止补位以保持保留集合是权重前缀
                break

    dropped = [_drop(unit, "budget:quota") for unit in units if unit.unit_id not in selected]

    while used > available and kept:
        victim = min(kept, key=lambda u: u.weight)
        kept.remove(victim)
        dropped.append(_drop(victim, "budget:overflow"))
        used -= victim.tokens
        layer_tokens[victim.layer] -= victim.tokens
        layer_units[victim.layer] -= 1

    return BudgetPlan(
        kept=kept,
        dropped=dropped,
        budget=available,
        effective=effective,
        fixed=fixed,
        layer_tokens=layer_tokens,
        layer_units=layer_units,
    )


def _guarantee_minimums(
    grouped: dict[str, list[ContextUnit]],
    kept: list[ContextUnit],
    effective: int,
    layer_tokens: dict,
    layer_units: dict,
) -> None:
    """保证最低可用性：证据至少 1 条、历史最近 2 条、画像最多 3 条、任务全部。"""
    selected = {unit.unit_id for unit in kept}
    used = sum(unit.tokens for unit in kept)

    def add(layer: str, candidates: list[ContextUnit], limit: int | None = None) -> None:
        nonlocal used
        added = 0
        for unit in candidates:
            if limit is not None and added >= limit:
                break
            if unit.unit_id in selected:
                continue
            if used + unit.tokens > effective:
                continue
            kept.append(unit)
            selected.add(unit.unit_id)
            used += unit.tokens
            layer_tokens[layer] += unit.tokens
            layer_units[layer] += 1
            added += 1

    evidence = grouped.get("evidence", [])
    if not any(unit.layer == "evidence" for unit in kept) and evidence:
        add("evidence", sorted(evidence, key=lambda u: u.weight, reverse=True)[:1])

    history = sorted(grouped.get("history", []), key=lambda u: u.meta.get("message_id") or 0)
    recent = history[-2:] if history else []
    if sum(1 for unit in kept if unit.layer == "history") < len(recent):
        add("history", recent)

    task = grouped.get("task", [])
    if task and not any(unit.layer == "task" for unit in kept):
        add("task", sorted(task, key=lambda u: u.weight, reverse=True))

    persona = grouped.get("persona", [])
    if persona and sum(1 for unit in kept if unit.layer == "persona") < min(3, len(persona)):
        add("persona", sorted(persona, key=lambda u: u.weight, reverse=True), limit=3)
