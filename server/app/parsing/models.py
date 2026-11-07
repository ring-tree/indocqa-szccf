"""解析层数据模型。"""

from dataclasses import dataclass, field


@dataclass
class RawTable:
    """从文档中提取的原始表格（行为单元格列表，首行为表头）。"""

    rows: list[list[str]]
    page: int | None = None
    section: str = ""
    table_no: str | None = None


@dataclass
class Paragraph:
    """待切块的段落，保留所在页码与章节便于引用溯源。"""

    text: str
    page: int | None
    section: str
    seq: int = 0


@dataclass
class LoadedDoc:
    """加载后的文档：段落流 + 表格 + 文档元信息。"""

    doc_id: str
    filename: str
    paragraphs: list[Paragraph] = field(default_factory=list)
    tables: list[RawTable] = field(default_factory=list)
    pages: int | None = None


@dataclass
class ChildChunk:
    chunk_id: str
    parent_id: str
    doc_id: str
    text: str
    page: int | None
    section: str
    kind: str = "text"  # text | table
    table_no: str | None = None


@dataclass
class ParentChunk:
    parent_id: str
    doc_id: str
    section: str
    page_start: int | None
    text: str
