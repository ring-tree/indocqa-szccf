"""输出格式化：移除 Markdown 标记，同时保护公式、代码与特殊数据不被破坏。"""

import re

_MATH_PATTERNS = (
    re.compile(r"\$\$[\s\S]+?\$\$"),
    re.compile(r"(?<!\$)\$(?!\$)[^$\n]+?\$(?!\$)"),
    re.compile(r"\\\[[\s\S]+?\\\]"),
    re.compile(r"\\\([\s\S]+?\\\)"),
)
_FENCE = re.compile(r"```[^\n]*\n?([\s\S]*?)```")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_QUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_HR = re.compile(r"^\s{0,3}(?:(?:[-*_]\s*){3,}|={3,})\s*$", re.MULTILINE)
_TASK = re.compile(r"^\s{0,3}[-*+]\s+\[[ xX]\]\s+", re.MULTILINE)
_REF_LINK = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
_BOLD3 = re.compile(r"\*\*\*(.+?)\*\*\*", re.DOTALL)
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_UNDER_BOLD = re.compile(r"__(.+?)__", re.DOTALL)
_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)
_ITALIC_STAR = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_ITALIC_UNDER = re.compile(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])")
_BULLET = re.compile(r"^\s{0,3}[-*+]\s+", re.MULTILINE)
_ORDERED = re.compile(r"^\s{0,3}(\d+)[.)]\s+", re.MULTILINE)
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]{3,}\|[\s:|-]*\|?\s*$", re.MULTILINE)
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$", re.MULTILINE)
_HTML = re.compile(r"</?[A-Za-z][^>]*>")
_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\ufeff]")


def _protect(text: str) -> tuple[str, dict[str, str]]:
    placeholders: dict[str, str] = {}

    def save(value: str) -> str:
        key = f"\u0000MATH{len(placeholders)}\u0000"
        placeholders[key] = value
        return key

    for pattern in _MATH_PATTERNS:
        text = pattern.sub(lambda match: save(match.group(0)), text)
    return text, placeholders


def _table_row(match: re.Match) -> str:
    cells = [cell.strip() for cell in match.group(1).split("|")]
    return "，".join(cell for cell in cells if cell)


def strip_markdown(text: str) -> str:
    """把模型输出转为纯文本；公式、代码内容与乘号/下划线标识保持原样。"""
    if not text:
        return ""
    value = text.replace("\r\n", "\n").replace("\r", "\n")
    value = _ZERO_WIDTH.sub("", value)
    value, placeholders = _protect(value)

    value = _FENCE.sub(lambda match: match.group(1).strip("\n"), value)
    value = _INLINE_CODE.sub(r"\1", value)
    value = _IMAGE.sub(r"\1", value)
    value = _LINK.sub(r"\1", value)
    value = _REF_LINK.sub(r"\1", value)
    value = _HEADING.sub("", value)
    value = _QUOTE.sub("", value)
    value = _HR.sub("", value)
    value = _BOLD3.sub(r"\1", value)
    value = _BOLD.sub(r"\1", value)
    value = _UNDER_BOLD.sub(r"\1", value)
    value = _STRIKE.sub(r"\1", value)
    value = _ITALIC_STAR.sub(r"\1", value)
    value = _ITALIC_UNDER.sub(r"\1", value)
    value = _TABLE_SEP.sub("", value)
    value = _TABLE_ROW.sub(_table_row, value)
    value = _TASK.sub("", value)
    value = _BULLET.sub("", value)
    value = _ORDERED.sub(r"\1、", value)
    value = _HTML.sub("", value)
    value = value.replace("\\*", "*").replace("\\_", "_").replace("\\#", "#")

    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    for key, original in placeholders.items():
        value = value.replace(key, original)
    return value.strip()


def split_chunks(text: str, size: int = 32) -> list[str]:
    """把最终文本切成固定大小片段，以 token 事件继续下发。"""
    return [text[index : index + size] for index in range(0, len(text), size)] or [""]
