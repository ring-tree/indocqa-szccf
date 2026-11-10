"""引用约束：事实/数据句必须有 [n] 引用，支持确定性绑定与不可溯源内容剔除。"""

import re
from dataclasses import dataclass, field

from app.retrieval.searcher import Hit

_CITED = re.compile(r"\[(\d+)\]")
_DATA = re.compile(
    r"\d|MPa|kPa|℃|°C|小时|分钟|周期|压力|温度|型号|额定|流量|电压|电流|功率|mm|kg|rpm|%"
)
_CLAIM = re.compile(
    r"是指|表示|属于|包括|采用|要求|必须|应当|应|需要|不得|可以|为|是|没有|不存在|相同|一致|区别"
)
_REFUSAL = re.compile(r"现有资料未覆盖|无法根据|没有找到|资料不足")
_SENTENCE = re.compile(r"[^。！？!?\n]+[。！？!?]?")
_CLEAN = re.compile(r"[^0-9A-Za-z\u3400-\u9fff]+")
_LIST_PREFIX = re.compile(r"^\s*\d+\s*[)、.]\s*")
_ONLY_CITED = re.compile(r"^\s*\[\d+\]\s*[。！？!?]?\s*$")
_DUP_CITED = re.compile(r"(\[\d+\])([。！？!?]?)\s*\1")
_MOVE_TRAILING = re.compile(r"([。！？!?])\s*((?:\[\d+\])+)")


@dataclass
class CitationAudit:
    citations: list[int] = field(default_factory=list)
    data_facts: int = 0
    data_cited: int = 0
    data_removed: int = 0
    claims: int = 0
    claims_cited: int = 0
    claims_bound: int = 0
    claims_removed: int = 0
    invalid_removed: int = 0

    @property
    def coverage(self) -> float:
        return self.data_cited / self.data_facts if self.data_facts else 1.0

    @property
    def claim_coverage(self) -> float:
        return self.claims_cited / self.claims if self.claims else 1.0

    def to_dict(self) -> dict:
        return {
            "citations": self.citations,
            "dataFacts": self.data_facts,
            "dataCited": self.data_cited,
            "dataRemoved": self.data_removed,
            "claims": self.claims,
            "claimsCited": self.claims_cited,
            "claimsBound": self.claims_bound,
            "claimsRemoved": self.claims_removed,
            "invalidRemoved": self.invalid_removed,
            "coverage": round(self.coverage, 4),
            "claimCoverage": round(self.claim_coverage, 4),
        }


def _bigrams(text: str) -> set[str]:
    clean = _CLEAN.sub("", text)
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[index : index + 2] for index in range(len(clean) - 1)}


def _best_match(sentence: str, hits: list[Hit]) -> int | None:
    grams = _bigrams(_CITED.sub("", sentence))
    if not grams or not hits:
        return None
    best_index, best_score = None, 0.0
    for index, hit in enumerate(hits, start=1):
        target = _bigrams(hit.text)
        if not target:
            continue
        overlap = len(grams & target)
        score = overlap / max(1, len(grams))
        if score > best_score:
            best_index, best_score = index, score
    return best_index if best_score >= 0.12 and best_index is not None else None


def _append_citation(sentence: str, index: int) -> str:
    match = re.search(r"([。！？!?]+)$", sentence)
    if match:
        return f"{sentence[: match.start()]}[{index}]{match.group(1)}"
    return f"{sentence}[{index}]"


def enforce_citations(answer: str, hits: list[Hit]) -> tuple[str, CitationAudit]:
    """逐句审计：数据句必须可溯源；无法匹配来源的数据句直接剔除。"""
    audit = CitationAudit()
    if not answer:
        return answer, audit
    lines = []
    for line in answer.splitlines():
        if not line.strip():
            lines.append(line)
            continue
        rendered = []
        for sentence in _SENTENCE.findall(line):
            if not sentence.strip():
                continue
            if _ONLY_CITED.match(sentence):
                match = _CITED.search(sentence)
                if match and rendered and 1 <= int(match.group(1)) <= len(hits):
                    index = int(match.group(1))
                    rendered[-1] = _append_citation(rendered[-1], index)
                    audit.citations.append(index)
                continue  # 独立的 [n] 片段归属前句，避免重复输出
            before = sentence
            sentence = _CITED.sub(
                lambda match: match.group(0) if 1 <= int(match.group(1)) <= len(hits) else "",
                sentence,
            )
            if sentence != before:
                audit.invalid_removed += 1
            cited = [int(n) for n in _CITED.findall(sentence)]
            if cited:
                audit.citations.extend(cited)
            if _REFUSAL.search(sentence):
                rendered.append(sentence)
                continue
            clean_sentence = _CITED.sub("", _LIST_PREFIX.sub("", sentence))
            is_data = bool(_DATA.search(clean_sentence))
            is_claim = bool(_CLAIM.search(clean_sentence)) and len(clean_sentence) >= 4
            if not is_data and not is_claim:
                rendered.append(sentence)
                continue
            if is_data:
                audit.data_facts += 1
            if is_claim:
                audit.claims += 1
            if cited:
                if is_data:
                    audit.data_cited += 1
                if is_claim:
                    audit.claims_cited += 1
                rendered.append(sentence)
                continue
            index = _best_match(sentence, hits)
            if index is not None:
                rendered.append(_append_citation(sentence, index))
                audit.citations.append(index)
                if is_data:
                    audit.data_cited += 1
                if is_claim:
                    audit.claims_bound += 1
                continue
            if is_data:
                audit.data_removed += 1
                continue
            if is_claim:
                audit.claims_removed += 1
                continue
            rendered.append(sentence)
        lines.append("".join(rendered))
    audit.citations = sorted(set(audit.citations))
    text = _MOVE_TRAILING.sub(r"\2\1", "\n".join(lines))
    text = _DUP_CITED.sub(r"\1\2", text)
    return text.strip(), audit
