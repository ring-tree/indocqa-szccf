"""文档加载：PDF（PyMuPDF 文本 + 书签章节 + pdfplumber 表格）与 Markdown。"""

import hashlib
import re

import pdfplumber
import pymupdf

from app.core.exceptions import UnsupportedFormatError
from app.parsing.models import LoadedDoc, Paragraph, RawTable

MD_EXTS = {".md", ".markdown", ".txt"}

_HEADING_MD = re.compile(r"^(#{1,6})\s+(.+)$")
_HEADING_ZH = re.compile(r"^第[0-9一二三四五六七八九十百]+[章节部]\s*(.*)$")
_HEADING_NUM = re.compile(r"^\d+(?:\.\d+){0,3}[、.\s]\s*\S{2,30}$")
_TABLE_NO = re.compile(r"(表|Table|Figure for)\s*\d+[-–.\d]*", re.IGNORECASE)
_MD_TABLE_SEP = re.compile(r"^[\s|:-]+$")


def load_document(path) -> LoadedDoc:
    import pathlib

    p = pathlib.Path(path)
    suffix = p.suffix.lower()
    data = p.read_bytes()
    doc_id = hashlib.sha256(data).hexdigest()[:16]
    if suffix == ".pdf":
        return _load_pdf(p, doc_id)
    if suffix in MD_EXTS:
        return _load_md(p, doc_id)
    raise UnsupportedFormatError(f"不支持的文件格式：{suffix}", detail=str(p.name))


def _load_pdf(path, doc_id: str) -> LoadedDoc:
    doc = pymupdf.open(path)
    paragraphs: list[Paragraph] = []
    toc = doc.get_toc(simple=True)  # [level, title, page(1-based)]
    page_section: dict[int, str] = {}
    current = ""
    for _level, title, start_page in sorted(toc, key=lambda e: e[2]):
        if start_page <= doc.page_count:
            current = title.strip() or current
            page_section[start_page] = current
    # 无书签时用中文标题启发式兜底
    sections_by_page: list[str] = []
    heading = ""
    for i in range(doc.page_count):
        if i + 1 in page_section:
            heading = page_section[i + 1]
        text = doc[i].get_text("text")
        if not toc:
            for line in text.splitlines():
                s = line.strip()
                if len(s) < 30 and (_HEADING_ZH.match(s) or _HEADING_NUM.match(s)):
                    heading = s
                    break
        sections_by_page.append(heading)
        for para in _pdf_paragraphs(text):
            paragraphs.append(Paragraph(text=para, page=i + 1, section=heading))

    tables: list[RawTable] = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            section = sections_by_page[i] if i < len(sections_by_page) else ""
            for found in page.find_tables():
                rows = _clean_rows(found.extract())
                if len(rows) >= 2 and any(len(r) >= 2 for r in rows):
                    tables.append(
                        RawTable(
                            rows=rows,
                            page=i + 1,
                            section=section,
                            table_no=_table_no_above(page, found),
                        )
                    )
    return LoadedDoc(
        doc_id=doc_id,
        filename=path.name,
        paragraphs=paragraphs,
        tables=tables,
        pages=doc.page_count,
    )


_TOC_LINE = re.compile(r"\.{4,}\s*\d+\s*$|·{4,}\s*\d+\s*$")


def _is_toc_block(paragraph: str) -> bool:
    """目录页特征：过半数行是“标题....页码”样式，作为检索块只有噪声。"""
    lines = [l for l in paragraph.split(" ") if l.strip()]
    if len(lines) < 3:
        return False
    hits = sum(1 for l in paragraph.splitlines() if _TOC_LINE.search(l))
    return hits >= 2


def _pdf_paragraphs(text: str) -> list[str]:
    """按行聚合段落：列表项独立成段，避免编号列表被合并切断；过滤目录样式块。"""
    paras: list[str] = []
    buf: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            if buf:
                _push(paras, buf)
                buf = []
            continue
        if re.match(r"^(\d+[.、)]|[-*•]|[a-zA-Z]\))", s) and buf:
            _push(paras, buf)
            buf = []
        buf.append(s)
    if buf:
        _push(paras, buf)
    return [p for p in paras if len(p) >= 8]


def _push(paras: list[str], buf: list[str]) -> None:
    joined = " ".join(buf)
    if len(joined) >= 8 and not _is_toc_block(joined):
        paras.append(joined)


def _load_md(path, doc_id: str) -> LoadedDoc:
    text = path.read_text(encoding="utf-8")
    paragraphs: list[Paragraph] = []
    tables: list[RawTable] = []
    section = ""
    buf: list[str] = []
    table_buf: list[str] = []

    def flush() -> None:
        if buf:
            joined = "\n".join(buf).strip()
            if joined:
                paragraphs.append(Paragraph(text=joined, page=None, section=section))
                last_line[0] = buf[-1]
            buf.clear()

    last_line = [""]
    for line in text.splitlines():
        s = line.rstrip()
        if s.lstrip().startswith(">"):
            # 手册类语料中引用块多为声明/批注而非正文，不进入检索块
            continue
        if s.startswith("|"):
            flush()
            table_buf.append(s)
            continue
        if table_buf:
            rows = _parse_md_table(table_buf)
            if rows:
                caption = _TABLE_NO.search(last_line[0]) or _TABLE_NO.search(section)
                tables.append(
                    RawTable(
                        rows=rows,
                        page=None,
                        section=section,
                        table_no=caption.group(0) if caption else None,
                    )
                )
            table_buf = []
        m = _HEADING_MD.match(s.strip())
        if m:
            flush()
            section = m.group(2).strip()
            continue
        if not s.strip():
            flush()
            continue
        buf.append(s.strip())
    flush()
    if table_buf:
        rows = _parse_md_table(table_buf)
        if rows:
            caption = _TABLE_NO.search(last_line[0]) or _TABLE_NO.search(section)
            tables.append(
                RawTable(
                    rows=rows,
                    page=None,
                    section=section,
                    table_no=caption.group(0) if caption else None,
                )
            )
    return LoadedDoc(
        doc_id=doc_id, filename=path.name, paragraphs=paragraphs, tables=tables, pages=None
    )


def _parse_md_table(lines: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in lines:
        if _MD_TABLE_SEP.match(line.strip()) and "-" in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if any(cells):
            rows.append(cells)
    return rows


def _table_no_above(page, table) -> str | None:
    """取表格正上方邻近文本中的“表 X-X / Table X-X”题注；找不到则回退页内首个匹配。"""
    top = max(0, table.bbox[1] - 45)
    try:
        above = page.crop((0, top, page.width, table.bbox[1])).extract_text() or ""
    except Exception:  # noqa: BLE001 pdfplumber 裁剪可能抛出多种类型，题注缺失不应中断解析
        above = ""
    near = _TABLE_NO.search(above) or _TABLE_NO.search(page.extract_text() or "")
    return near.group(0) if near else None


def _clean_rows(raw) -> list[list[str]]:
    rows = []
    for r in raw or []:
        cells = [(c or "").replace("\n", " ").strip() for c in r]
        rows.append(cells)
    return rows
