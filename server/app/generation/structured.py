"""结构化输出：模型 JSON 解析、字段补全与稳定序列化。"""

import json
import re

from app.generation.citations import CitationAudit

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

JSON_INSTRUCTION = (
    "输出要求：只输出一个合法 JSON 对象，不要 Markdown、不要代码围栏、不要额外解释。"
    '格式为 {"answer": "纯文本回答，不含 Markdown 标记", "citations": [1, 2]}。'
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
    if citations is not None and not isinstance(citations, list):
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
) -> dict:
    indices = audit.citations if audit else []
    return {
        "schemaVersion": "1.0",
        "answer": answer,
        "citations": _citation_items(indices, sources),
        "confidence": confidence,
        "confidenceReason": confidence_reason,
        "boundary": boundary,
        "model": model,
        "traceId": trace_id,
        "citationCoverage": round(audit.coverage, 4) if audit else None,
    }


def dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
