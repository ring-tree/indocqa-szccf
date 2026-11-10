"""AI 辅助分类：规则 + 目录语义近邻 + LLM 复核，输出可解释的 Top3 候选。

设计约束：
- 未配置输出层模型时整体不可用（由 API 返回 40904），前端按钮禁用；
- AI 只给建议，任何写操作仍由用户确认；
- LLM 超时或解析失败时降级为规则+语义结果，不阻塞入库。
"""

import asyncio
import json
import re
import time

from app.core.config import get_settings
from app.core.exceptions import AIUnavailableError, DocumentNotFoundError
from app.core.llm import LLMGateway
from app.indexing import store
from app.indexing.folders import DEFAULT_FOLDER
from app.indexing.manifest import Manifest

MAX_SUMMARY = 800
LLM_TIMEOUT = 8.0
HIGH, MID = 0.80, 0.60

_WEIGHTS = {"semantic": 0.40, "rule": 0.25, "title": 0.15, "structure": 0.10, "history": 0.10}
_RULES = (
    (("fixture", "评测夹具"), ("评测夹具",), "文件名命中评测夹具"),
    (("sourcebook", "compressed_air", "doe"), ("行业标准",), "行业标准/能源类资料"),
    (("螺杆", "screw"), ("螺杆",), "文件名命中螺杆空压机"),
    (("无油", "静音"), ("无油",), "文件名命中无油/静音空压机"),
    (("安全", "规程"), ("安全",), "文档属于安全规程"),
)
_JSON = re.compile(r"\{.*\}", re.DOTALL)


def is_available(settings=None) -> bool:
    """AI 分类的唯一开关：输出层模型已配置。"""
    return LLMGateway(settings or get_settings()).configured


def _level(score: float) -> str:
    if score >= HIGH:
        return "high"
    if score >= MID:
        return "mid"
    return "low"


def _clip(value: float) -> float:
    return max(0.0, min(1.0, value))


def _normalize_sim(sim: float) -> float:
    """把余弦相似度映射到 0–1；0.35 以下视为不相关。"""
    return _clip((sim - 0.35) / 0.35)


def _rule_match(filename: str, folders: list[str]) -> tuple[str | None, str]:
    name = (filename or "").lower()
    for keys, suffixes, reason in _RULES:
        if not any(k.lower() in name for k in keys):
            continue
        candidates = [
            path for path in folders if path != DEFAULT_FOLDER and any(s in path for s in suffixes)
        ]
        if candidates:
            return max(candidates, key=len), reason
    return None, ""


def _title_score(filename: str, path: str) -> float:
    name = (filename or "").lower()
    segments = [s.lower() for s in path.split("/") if s]
    if any(len(s) >= 2 and s in name for s in segments):
        return 1.0
    if any(word in name for word in ("手册", "说明书", "标准", "规程", "指南")):
        return 0.6
    return 0.2


def _structure_score(filename: str, text: str) -> float:
    name = (filename or "").lower()
    if any(word in name for word in ("手册", "说明书", "标准", "sourcebook")):
        return 0.8
    if len(text) > 3000 and any(word in text for word in ("表", "Table", "参数")):
        return 0.7
    return 0.5


def _semantic_scores(
    settings, vector: list[float] | None, exclude_doc_id: str | None
) -> tuple[dict[str, float], dict[str, list[str]]]:
    if not vector:
        return {}, {}
    try:
        neighbors = store.folder_neighbors(
            settings, vector, exclude_doc_id=exclude_doc_id, top_n=60
        )
    except Exception:  # noqa: BLE001 语义路失败时降级为规则
        return {}, {}
    scores: dict[str, float] = {}
    evidence: dict[str, list[str]] = {}
    for item in neighbors:
        folder = item["folder"]
        if folder == DEFAULT_FOLDER:
            continue
        scores[folder] = max(scores.get(folder, 0.0), item["sim"])
        sections = evidence.setdefault(folder, [])
        section = item.get("section") or ""
        if section and section not in sections and len(sections) < 3:
            sections.append(section)
    return {folder: _normalize_sim(sim) for folder, sim in scores.items()}, evidence


def _build_candidates(
    folders: list[str],
    filename: str,
    text: str,
    semantic: dict[str, float],
    evidence: dict[str, list[str]],
) -> list[dict]:
    rule_folder, rule_reason = _rule_match(filename, folders)
    candidates = []
    for path in folders:
        if path == DEFAULT_FOLDER:
            continue
        sem = semantic.get(path, 0.0)
        rule = 1.0 if path == rule_folder else 0.2
        title = _title_score(filename, path)
        structure = _structure_score(filename, text)
        history = 0.5
        score = (
            _WEIGHTS["semantic"] * sem
            + _WEIGHTS["rule"] * rule
            + _WEIGHTS["title"] * title
            + _WEIGHTS["structure"] * structure
            + _WEIGHTS["history"] * history
        )
        if path == rule_folder:
            score = max(score, 0.92)
            reason = rule_reason
        elif sem > 0:
            reason = f"与目录内文档语义最接近（相似度 {sem * 0.35 + 0.35:.2f}）"
        else:
            reason = "目录名称/类型匹配"
        candidates.append(
            {
                "path": path,
                "confidence": round(_clip(score), 4),
                "reason": reason,
                "evidence": evidence.get(path, [])[:3],
            }
        )
    candidates.sort(key=lambda item: item["confidence"], reverse=True)
    return candidates


async def _llm_rerank(
    settings,
    gateway: LLMGateway,
    filename: str,
    summary: str,
    candidates: list[dict],
    folders: list[str],
) -> tuple[list[dict], bool]:
    """低置信时让模型复核；成功返回合并后的候选，失败返回原候选并标记降级。"""
    if not candidates:
        return candidates, True
    folder_lines = "\n".join(
        f"- {item['path']}（当前置信度 {item['confidence']:.2f}）" for item in candidates[:8]
    )
    prompt = (
        "你是工业知识库归档助手。请从候选目录中为文档选择最合适的目录。\n"
        f"候选目录：\n{folder_lines}\n\n"
        f"文档文件名：{filename}\n文档摘要：{summary[:600]}\n\n"
        '只输出 JSON：{"candidates":[{"path":"候选目录之一","confidence":0到1,"reason":"20字内依据"}]}，'
        "按置信度降序，最多 3 个；path 必须来自候选目录。"
    )
    try:
        text = await asyncio.wait_for(
            gateway.complete([{"role": "user", "content": prompt}]), timeout=LLM_TIMEOUT
        )
        match = _JSON.search(text or "")
        payload = json.loads(match.group(0)) if match else {}
        items = payload.get("candidates") or []
    except Exception:  # noqa: BLE001 模型输出不可解析时保持原候选
        return candidates, True

    allowed = {path for path in folders}
    merged = {item["path"]: dict(item) for item in candidates}
    for item in items:
        path = str(item.get("path", "")).strip()
        if path not in allowed:
            continue
        try:
            score = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            continue
        reason = str(item.get("reason", ""))[:40] or "模型复核推荐"
        current = merged.get(path)
        if current:
            current["confidence"] = round(0.6 * current["confidence"] + 0.4 * _clip(score), 4)
            current["reason"] = reason
        else:
            merged[path] = {
                "path": path,
                "confidence": round(0.55 * _clip(score), 4),
                "reason": reason,
                "evidence": [],
            }
    result = sorted(merged.values(), key=lambda item: item["confidence"], reverse=True)
    return result[:3], False


async def classify_text(
    settings,
    filename: str,
    text: str,
    *,
    use_llm: bool = True,
    exclude_doc_id: str | None = None,
) -> dict:
    """对一段文本（预览）做分类；未配置模型时抛出 40904。"""
    settings = settings or get_settings()
    gateway = LLMGateway(settings)
    if not gateway.configured:
        raise AIUnavailableError("无可用模型")
    started = time.perf_counter()
    manifest = Manifest(Manifest.path_for(settings))
    folders = manifest.folders()
    if not folders:
        return {
            "available": True,
            "candidates": [],
            "fallback": False,
            "reason": "no_folders",
            "latencyMs": int((time.perf_counter() - started) * 1000),
        }
    summary = f"{filename}\n{text[:MAX_SUMMARY]}"
    try:
        vector = store.query_embedding(settings, summary)
    except Exception:  # noqa: BLE001 嵌入不可用时只走规则
        vector = None
    semantic, evidence = _semantic_scores(settings, vector, exclude_doc_id)
    candidates = _build_candidates(folders, filename, text, semantic, evidence)
    fallback = vector is None
    if use_llm and candidates:
        top = candidates[0]["confidence"]
        margin = top - candidates[1]["confidence"] if len(candidates) > 1 else 1.0
        if top < 0.72 or margin < 0.08:
            candidates, fallback = await _llm_rerank(
                settings, gateway, filename, summary, candidates, folders
            )
    for item in candidates:
        item["level"] = _level(item["confidence"])
    return {
        "available": True,
        "candidates": candidates[:3],
        "fallback": fallback,
        "reason": "",
        "latencyMs": int((time.perf_counter() - started) * 1000),
        "model": gateway.last_model,
    }


async def classify_document(settings, doc_id: str) -> dict:
    settings = settings or get_settings()
    manifest = Manifest(Manifest.path_for(settings))
    info = manifest.get(doc_id)
    if not info:
        raise DocumentNotFoundError(f"文档不存在: {doc_id}")
    profile = store.doc_profile(settings, doc_id)
    text = "\n".join(profile.get("texts") or [])
    filename = info.get("filename", "")
    gateway = LLMGateway(settings)
    if not gateway.configured:
        raise AIUnavailableError("无可用模型")
    started = time.perf_counter()
    folders = manifest.folders()
    if not folders:
        return {
            "available": True,
            "candidates": [],
            "fallback": False,
            "reason": "no_folders",
            "latencyMs": int((time.perf_counter() - started) * 1000),
        }
    vector = profile.get("vector")
    if not vector:
        try:
            vector = store.query_embedding(settings, f"{filename}\n{text[:MAX_SUMMARY]}")
        except Exception:  # noqa: BLE001
            vector = None
    semantic, evidence = _semantic_scores(settings, vector, doc_id)
    candidates = _build_candidates(folders, filename, text, semantic, evidence)
    fallback = vector is None
    if candidates:
        top = candidates[0]["confidence"]
        margin = top - candidates[1]["confidence"] if len(candidates) > 1 else 1.0
        if top < 0.72 or margin < 0.08:
            candidates, fallback = await _llm_rerank(
                settings, gateway, filename, text[:MAX_SUMMARY], candidates, folders
            )
    for item in candidates:
        item["level"] = _level(item["confidence"])
    return {
        "available": True,
        "candidates": candidates[:3],
        "fallback": fallback,
        "reason": "",
        "latencyMs": int((time.perf_counter() - started) * 1000),
        "model": gateway.last_model,
    }


def append_feedback(settings, payload: dict) -> None:
    import pathlib

    path = pathlib.Path(settings.chroma_dir) / "classification_feedback.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "docId": payload.get("docId"),
        "suggested": payload.get("suggested"),
        "chosen": payload.get("chosen"),
        "action": payload.get("action"),
        "source": payload.get("source"),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
