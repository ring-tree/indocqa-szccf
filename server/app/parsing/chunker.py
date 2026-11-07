"""parent-child 两级切块：子块约 400–900 字符，父块=小节，表格独立成块。"""

import re

from app.parsing.models import ChildChunk, LoadedDoc, ParentChunk, RawTable

TARGET = 600
HARD_MAX = 900
MIN_TAIL = 120
_SENT_SPLIT = re.compile(r"(?<=[。！？；!?;])\s+")


def serialize_table(table: RawTable) -> str:
    """表格转可检索文本：首行为表头，行按"列名=值"描述。"""
    if not table.rows:
        return ""
    header = table.rows[0]
    lines = ["表头: " + " | ".join(header)]
    for idx, row in enumerate(table.rows[1:], start=1):
        pairs = []
        for col_idx, cell in enumerate(row):
            name = header[col_idx] if col_idx < len(header) else f"列{col_idx + 1}"
            if cell:
                pairs.append(f"{name}={cell}")
        if pairs:
            lines.append(f"行{idx}: " + "; ".join(pairs))
    label = f"{table.table_no} " if table.table_no else ""
    return f"【{label}表格】" + "\n".join(lines)


def build_chunks(doc: LoadedDoc) -> tuple[list[ChildChunk], list[ParentChunk]]:
    """按小节聚合：段落流与表格按原顺序统一编号，表格单独成子块。"""
    items: list[tuple] = []
    seq = 0
    for para in doc.paragraphs:
        items.append(("text", para.text, para.page, para.section, seq))
        seq += 1
    for table in doc.tables:
        items.append(("table", serialize_table(table), table.page, table.section, seq))
        seq += 1
    items.sort(key=lambda it: it[4])

    children: list[ChildChunk] = []
    parents: list[ParentChunk] = []
    section_groups: dict[str, list] = {}
    for kind, text, page, section, _ in items:
        section_groups.setdefault(section or "未分节", []).append((kind, text, page))

    parent_counter = 0
    for section, group in section_groups.items():
        parent_id = f"{doc.doc_id}-p{parent_counter:03d}"
        parent_counter += 1
        pages = [p for _k, _t, p in group if p is not None]
        packed = _pack(group)
        child_texts = []
        for text, page, kind, table_no in packed:
            chunk_id = f"{doc.doc_id}-c{len(children):04d}"
            children.append(
                ChildChunk(
                    chunk_id=chunk_id,
                    parent_id=parent_id,
                    doc_id=doc.doc_id,
                    text=text,
                    page=page,
                    section=section,
                    kind=kind,
                    table_no=table_no,
                )
            )
            child_texts.append(text)
        parents.append(
            ParentChunk(
                parent_id=parent_id,
                doc_id=doc.doc_id,
                section=section,
                page_start=min(pages) if pages else None,
                text="\n".join(child_texts),
            )
        )
    return children, parents


def _pack(group: list) -> list[tuple[str, int | None, str, str | None]]:
    """把 (kind, text, page) 组打包为子块列表；表格独立、段落合并，段落边界即切块边界。"""
    packed: list[tuple[str, int | None, str, str | None]] = []
    buf: list[str] = []
    buf_page: int | None = None
    for kind, text, page in group:
        if kind == "table":
            if buf:
                packed.append(("\n".join(buf), buf_page, "text", None))
                buf = []
            packed.append((text, page, "table", _table_no_of(text)))
            continue
        for piece in _split_long(text):
            if buf and len("\n".join(buf)) + len(piece) > HARD_MAX:
                packed.append(("\n".join(buf), buf_page, "text", None))
                buf = []
                buf_page = None
            buf.append(piece)
            buf_page = buf_page if buf_page is not None else page
    if buf:
        tail = "\n".join(buf)
        if len(tail) < MIN_TAIL and packed and packed[-1][2] == "text":
            merged = packed[-1][0] + "\n" + tail
            if len(merged) <= HARD_MAX * 2:
                packed[-1] = (merged, packed[-1][1], "text", None)
        else:
            packed.append((tail, buf_page, "text", None))
    return packed


def _split_long(text: str) -> list[str]:
    if len(text) <= HARD_MAX:
        return [text]
    parts = _SENT_SPLIT.split(text)
    out: list[str] = []
    cur = ""
    for part in parts:
        if cur and len(cur) + len(part) > HARD_MAX:
            out.append(cur)
            cur = part
        else:
            cur = cur + part if cur else part
    if cur:
        out.append(cur)
    return out


def _table_no_of(text: str) -> str | None:
    if not text.startswith("【"):
        return None
    end = text.find("】")
    if end == -1:
        return None
    label = text[1:end].replace("表格", "").strip()
    return label or None
