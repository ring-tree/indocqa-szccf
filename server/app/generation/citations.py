"""引用约束：Markdown 感知的事实/数据句引用审计、自动绑定与不可溯源内容剔除。"""

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
_LIST_MARK = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)(.*)$")
_QUOTE_MARK = re.compile(r"^(\s*>\s?)(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]{3,}\|[\s:|-]*\|?\s*$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
_ONLY_CITED = re.compile(r"^\s*\[\d+\]\s*[。！？!?]?\s*$")
_DUP_CITED = re.compile(r"(\[\d+\])([。！？!?]?)\s*\1")
_MOVE_TRAILING = re.compile(r"([。！？!?])\s*((?:\[\d+\])+)")
_INLINE_MARKS = re.compile(r"\*\*|__|~~|`")
_PROTECTED = re.compile(
    r"(?s)(```.*?```|~~~.*?~~~|`[^`\n]+`|\$\$.*?\$\$|\\\[.*?\\\]|\\\(.*?\\\)|"
    r"(?<!\$)\$(?!\$)[^$\n]+?\$(?!\$))"
)
_CITE_TAIL = re.compile(r"([。！？!?]+)((?:\*\*|__|\*|_)?)\s*$")
_PLACEHOLDER = re.compile(r"\x00CITE\d+\x00")


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
    table_rows: int = 0
    table_rows_cited: int = 0

    @property
    def coverage(self) -> float:
        return self.data_cited / self.data_facts if self.data_facts else 1.0

    @property
    def claim_coverage(self) -> float:
        cited = self.claims_cited + self.claims_bound
        return cited / self.claims if self.claims else 1.0

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
            "tableRows": self.table_rows,
            "tableRowsCited": self.table_rows_cited,
            "coverage": round(self.coverage, 4),
            "claimCoverage": round(self.claim_coverage, 4),
        }


def _bigrams(text: str) -> set[str]:
    clean = re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", text or "")
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
    match = _CITE_TAIL.search(sentence)
    if match:
        return f"{sentence[: match.start()]}[{index}]{match.group(1)}{match.group(2)}"
    return f"{sentence}[{index}]"


def _clean_invalid(sentence: str, limit: int, audit: CitationAudit) -> str:
    def replace(match: re.Match) -> str:
        value = int(match.group(1))
        if 1 <= value <= limit:
            return match.group(0)
        audit.invalid_removed += 1
        return ""

    return _CITED.sub(replace, sentence)


def _append_table_citation(line: str, index: int) -> str:
    stripped = line.strip()
    if not stripped.startswith("|"):
        stripped = f"|{stripped}"
    if not stripped.endswith("|"):
        stripped = f"{stripped}|"
    cells = stripped.strip("|").split("|")
    for position in range(len(cells) - 1, -1, -1):
        if cells[position].strip():
            cells[position] = f"{cells[position].rstrip()} [{index}]"
            break
    return "|" + "|".join(cells) + "|"


def _analyze(sentence: str) -> tuple[bool, bool]:
    text = _INLINE_MARKS.sub("", _CITED.sub("", _PLACEHOLDER.sub("", sentence)))
    is_data = bool(_DATA.search(text))
    is_claim = bool(_CLAIM.search(text)) and len(text.strip()) >= 4
    return is_data, is_claim


def _process_sentence(
    sentence: str, hits: list[Hit], audit: CitationAudit, list_prefix: str = ""
) -> str | None:
    if not sentence.strip():
        return list_prefix or None
    if _ONLY_CITED.match(sentence):
        match = _CITED.search(sentence)
        if match and 1 <= int(match.group(1)) <= len(hits):
            audit.citations.append(int(match.group(1)))
        return None
    sentence = _clean_invalid(sentence, len(hits), audit)
    cited = [int(n) for n in _CITED.findall(sentence)]
    if cited:
        audit.citations.extend(cited)
    if _REFUSAL.search(sentence):
        return f"{list_prefix}{sentence}"
    is_data, is_claim = _analyze(sentence)
    if not is_data and not is_claim:
        return f"{list_prefix}{sentence}"
    if is_data:
        audit.data_facts += 1
    if is_claim:
        audit.claims += 1
    if cited:
        if is_data:
            audit.data_cited += 1
        if is_claim:
            audit.claims_cited += 1
        return f"{list_prefix}{sentence}"
    index = _best_match(sentence, hits)
    if index is not None:
        audit.citations.append(index)
        if is_data:
            audit.data_cited += 1
        if is_claim:
            audit.claims_bound += 1
        return f"{list_prefix}{_append_citation(sentence, index)}"
    if is_data:
        audit.data_removed += 1
    if is_claim:
        audit.claims_removed += 1
    return None


def _process_text(text: str, hits: list[Hit], audit: CitationAudit) -> str:
    rendered = []
    for sentence in _SENTENCE.findall(text):
        result = _process_sentence(sentence, hits, audit)
        if result:
            rendered.append(result)
    return "".join(rendered)


def _process_table(
    line: str,
    hits: list[Hit],
    audit: CitationAudit,
    header: bool,
    separator: bool,
) -> str | None:
    if separator or header:
        return line
    sentence = re.sub(r"^\s*\|?|\|?\s*$", "", line)
    audit.table_rows += 1
    cited = [int(n) for n in _CITED.findall(sentence)]
    valid = [n for n in cited if 1 <= n <= len(hits)]
    if valid:
        audit.table_rows_cited += 1
        audit.citations.extend(valid)
        return line
    if not _DATA.search(_INLINE_MARKS.sub("", sentence)):
        return line
    audit.data_facts += 1
    index = _best_match(sentence, hits)
    if index is None:
        audit.data_removed += 1
        return None
    audit.data_cited += 1
    audit.table_rows_cited += 1
    audit.citations.append(index)
    return _append_table_citation(line, index)


def _protect(text: str) -> tuple[str, dict[str, str]]:
    placeholders: dict[str, str] = {}

    def save(match: re.Match) -> str:
        key = f"\u0000CITE{len(placeholders)}\u0000"
        placeholders[key] = match.group(0)
        return key

    return _PROTECTED.sub(save, text), placeholders


def _restore(text: str, placeholders: dict[str, str]) -> str:
    for key, value in placeholders.items():
        text = text.replace(key, value)
    return text


def enforce_citations(answer: str, hits: list[Hit]) -> tuple[str, CitationAudit]:
    """逐句审计 Markdown 回答：数据句/事实句必须可溯源；无法匹配的内容剔除。"""
    audit = CitationAudit()
    if not answer:
        return answer, audit
    protected, placeholders = _protect(answer)
    lines = protected.split("\n")
    headers: set[int] = set()
    for index, line in enumerate(lines):
        if not _TABLE_ROW.match(line):
            continue
        for next_index in range(index + 1, len(lines)):
            if not lines[next_index].strip():
                continue
            if _TABLE_SEP.match(lines[next_index]):
                headers.add(index)
            break

    output: list[str] = []
    for index, line in enumerate(lines):
        if not line.strip():
            output.append(line)
            continue
        if _HEADING.match(line):
            output.append(line)
            continue
        if _TABLE_SEP.match(line):
            output.append(line)
            continue
        if _TABLE_ROW.match(line):
            row = _process_table(line, hits, audit, index in headers, False)
            if row is not None:
                output.append(row)
            continue
        list_match = _LIST_MARK.match(line)
        quote_match = _QUOTE_MARK.match(line)
        if list_match:
            prefix, body = list_match.group(1), list_match.group(2)
            rendered = _process_text(body, hits, audit)
            if rendered.strip():
                output.append(f"{prefix}{rendered}")
            continue
        if quote_match:
            prefix, body = quote_match.group(1), quote_match.group(2)
            rendered = _process_text(body, hits, audit)
            if rendered.strip():
                output.append(f"{prefix}{rendered}")
            continue
        rendered = _process_text(line, hits, audit)
        if rendered.strip():
            output.append(rendered)

    audit.citations = sorted(set(audit.citations))
    text = _MOVE_TRAILING.sub(r"\2\1", "\n".join(output))
    text = _DUP_CITED.sub(r"\1\2", text)
    return _restore(text, placeholders).strip(), audit
