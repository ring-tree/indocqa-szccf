"""输出格式化：保留合法 Markdown/公式/代码，清理模型产生的转义与冗余标记。"""

import re

_PROTECTED = re.compile(
    r"(?s)("
    r"```.*?```|~~~.*?~~~|`[^`\n]+`|"
    r"\$\$.*?\$\$|\\\[.*?\\\]|\\\(.*?\\\)|(?<!\$)\$(?!\$)[^$\n]+?\$(?!\$)"
    r")"
)
_CODE_ONLY = re.compile(r"(?s)(```.*?```|~~~.*?~~~|`[^`\n]+`)")
_MATH_BRACKET = re.compile(r"\\\[([\s\S]+?)\\\]")
_MATH_PAREN = re.compile(r"\\\(([\s\S]+?)\\\)")
_MATH_HINT = re.compile(r"[A-Za-z\\^_=+\-*/<>]")
_OUTER_FENCE = re.compile(r"^\s*```(?:markdown|md)[ \t]*\n([\s\S]*?)\n```\s*$", re.IGNORECASE)
_HTML_COMMENT = re.compile(r"<!--[\s\S]*?-->")
_ESCAPED_MARK = re.compile(r"\\([\\`*_{}\[\]()#+\-.!>|~])")
_EMPTY_EMPHASIS = re.compile(r"(?<!\*)\*\*[ \t]*\*\*(?!\*)|__[ \t]*__|~~[ \t]*~~")
_EMPTY_STAR = re.compile(r"(?m)^[ \t]*\*[ \t]+\*[ \t]*$")
_EMPTY_LINK = re.compile(r"\[\s*\]\([^)]*\)|!\[\s*\]\([^)]*\)")
_CITE_SPACE = re.compile(r"\[\s*(\d+)\s*\]")
_DUP_CITE = re.compile(r"(\[\d+\])(?:\s*\1)+")
_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\ufeff]")
_NUL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

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
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|[ \t]*$", re.MULTILINE)
_HTML = re.compile(r"</?[A-Za-z][^>]*>")


def _looks_like_math(inner: str) -> bool:
    return bool(_MATH_HINT.search(inner or ""))


def _normalize_math_delimiters(text: str) -> str:
    """把 \\(...\\) / \\[...\\] 统一为 $...$ / $$...$$；纯数字引用不改写。"""

    def bracket(match: re.Match) -> str:
        inner = match.group(1)
        return f"$${inner}$$" if _looks_like_math(inner) else match.group(0)

    def paren(match: re.Match) -> str:
        inner = match.group(1)
        return f"${inner}$" if _looks_like_math(inner) else match.group(0)

    return _MATH_PAREN.sub(paren, _MATH_BRACKET.sub(bracket, text))


def _protect_with(text: str, pattern, prefix: str, placeholders: dict[str, str]) -> str:
    def save(match: re.Match) -> str:
        key = f"\u0000{prefix}{len(placeholders)}\u0000"
        placeholders[key] = match.group(0)
        return key

    return pattern.sub(save, text)


def _protect(text: str) -> tuple[str, dict[str, str]]:
    placeholders: dict[str, str] = {}
    value = _protect_with(text, _PROTECTED, "MD", placeholders)
    return value, placeholders


def _restore(text: str, placeholders: dict[str, str]) -> str:
    for key, value in placeholders.items():
        text = text.replace(key, value)
    return text


def normalize_markdown(text: str) -> str:
    """保留合法 Markdown，清理转义残留、空标记、零宽字符与冗余空行。"""
    if not text:
        return ""
    value = text.replace("\r\n", "\n").replace("\r", "\n")
    value = _ZERO_WIDTH.sub("", value)
    value = _NUL.sub("", value)
    match = _OUTER_FENCE.match(value)
    if match:
        value = match.group(1)
    value = _HTML_COMMENT.sub("", value)
    placeholders: dict[str, str] = {}
    value = _protect_with(value, _CODE_ONLY, "CODE", placeholders)
    value = _normalize_math_delimiters(value)
    value, math_placeholders = _protect(value)
    placeholders.update(math_placeholders)

    value = _ESCAPED_MARK.sub(r"\1", value)
    value = _EMPTY_EMPHASIS.sub("", value)
    value = _EMPTY_STAR.sub("", value)
    value = _EMPTY_LINK.sub("", value)
    value = _CITE_SPACE.sub(r"[\1]", value)
    value = _DUP_CITE.sub(r"\1", value)

    lines = []
    for line in value.split("\n"):
        line = re.sub(r"[ \t]+$", "", line)
        line = re.sub(r"(?<=\S)[ \t]{2,}(?=\S)", " ", line)
        lines.append(line)
    value = "\n".join(lines)
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = _restore(value, placeholders)
    return value.strip()


def _math_to_plain(value: str) -> str:
    if value.startswith("$$") and value.endswith("$$"):
        return value[2:-2].strip()
    if value.startswith("$") and value.endswith("$"):
        return value[1:-1].strip()
    if value.startswith("\\[") and value.endswith("\\]"):
        return value[2:-2].strip()
    if value.startswith("\\(") and value.endswith("\\)"):
        return value[2:-2].strip()
    return value


def _code_to_plain(value: str) -> str:
    value = _FENCE.sub(lambda match: match.group(1).strip("\n"), value)
    return _INLINE_CODE.sub(r"\1", value)


def to_plain_text(text: str) -> str:
    """转为无格式 TXT：去掉 Markdown 标记、公式定界符，保留正文、表格数据与引用编号。"""
    if not text:
        return ""
    value = text.replace("\r\n", "\n").replace("\r", "\n")
    value = _ZERO_WIDTH.sub("", value)
    placeholders: dict[str, str] = {}
    value = _protect_with(value, _CODE_ONLY, "CODE", placeholders)
    value = _normalize_math_delimiters(value)
    value, math_placeholders = _protect(value)
    placeholders.update(math_placeholders)

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
    value = _TABLE_ROW.sub(
        lambda match: "，".join(cell.strip() for cell in match.group(1).split("|") if cell.strip()),
        value,
    )
    value = _TASK.sub("", value)
    value = _BULLET.sub("", value)
    value = _ORDERED.sub(r"\1、", value)
    value = _HTML.sub("", value)
    value = value.replace("\\*", "*").replace("\\_", "_").replace("\\#", "#")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    for key, original in placeholders.items():
        replacement = (
            _code_to_plain(original) if key.startswith("\u0000CODE") else _math_to_plain(original)
        )
        value = value.replace(key, replacement)
    return value.strip()


def split_chunks(text: str, size: int = 32) -> list[str]:
    """把最终文本切成固定大小片段，以 token 事件继续下发。"""
    return [text[index : index + size] for index in range(0, len(text), size)] or [""]
