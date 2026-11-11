"""结构化输出：模型 JSON 解析、字段补全与稳定序列化。"""

import json
import re

from app.generation.citations import CitationAudit

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

JSON_INSTRUCTION = (
    "输出要求：只输出一个合法 JSON 对象，不要代码围栏、不要 JSON 之外的任何解释。"
    '格式为 {"answer": "回答正文，可包含 Markdown 与 [n] 引用", "citations": [1, 2]}。'
    "answer 必须是 JSON 字符串，换行与引号必须正确转义。"
)

STRICT_JSON_INSTRUCTION = (
    "上次输出不是合法 JSON。请严格只输出一个 JSON 对象，字段 answer 为字符串、"
    "citations 为引用编号数组；不得包含任何 JSON 之外的文字或代码围栏。"
)


def parse_model_json(text: str) -> dict | None:
    value = (text or "").strip()
    value = _FENCE.sub("", value).strip()
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(value[start : end + 1])
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("answer"), str):
        return None
    citations = data.get("citations")
    if citations is not None and (
        not isinstance(citations, list) or not all(isinstance(item, int) for item in citations)
    ):
        return None
    return data


def _citation_items(indices: list[int], sources: list[dict]) -> list[dict]:
    by_index = {int(item.get("index", 0)): item for item in sources}
    out = []
    for index in sorted(set(indices)):
        item = by_index.get(index)
        if not item:
            continue
        out.append(
            {
                "index": index,
                "docId": item.get("docId"),
                "docName": item.get("docName"),
                "section": item.get("section"),
                "page": item.get("page"),
                "locator": item.get("locator"),
                "kind": item.get("kind"),
                "attachmentId": item.get("attachmentId"),
                "quote": (item.get("snippet") or "")[:240],
            }
        )
    return out


def build_payload(
    answer: str,
    sources: list[dict],
    confidence: str | None,
    confidence_reason: str,
    boundary: str | None,
    model: str | None,
    audit: CitationAudit | None = None,
    trace_id: str | None = None,
    answer_plain: str | None = None,
    safety: dict | None = None,
    generation_params: dict | None = None,
) -> dict:
    indices = audit.citations if audit else []
    return {
        "schemaVersion": "1.1",
        "answer": answer,
        "answerPlain": answer_plain if answer_plain is not None else answer,
        "citations": _citation_items(indices, sources),
        "confidence": confidence,
        "confidenceReason": confidence_reason,
        "boundary": boundary,
        "model": model,
        "traceId": trace_id,
        "citationCoverage": round(audit.coverage, 4) if audit else None,
        "citationAudit": audit.to_dict() if audit else None,
        "safety": safety or {"blocked": False, "hits": [], "categories": []},
        "generationParams": generation_params or {},
    }


def dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def is_valid_json(text: str) -> bool:
    try:
        json.loads(text)
    except (TypeError, ValueError):
        return False
    return True
