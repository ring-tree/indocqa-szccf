"""附件解析：文档文本、图片/音视频元数据。

解析失败不阻断上传，统一降级为 metadata_only，由前端提示“可预览/下载，未提取文本”。
"""

import csv
import io
import pathlib
import re
from dataclasses import dataclass, field

from app.attachments.constants import extension_of, kind_of


@dataclass
class ParseResult:
    text: str | None = None
    meta: dict = field(default_factory=dict)
    parse_status: str = "ok"


def extract(path: pathlib.Path, settings) -> ParseResult:
    suffix = extension_of(path.name)
    kind = kind_of(path.name)
    try:
        if suffix == ".pdf":
            return _pdf(path, settings)
        if suffix in {".md", ".markdown", ".txt"}:
            return _markdown_or_text(path, settings)
        if suffix == ".docx":
            return _docx(path, settings)
        if suffix == ".csv":
            return _csv(path, settings)
        if suffix == ".xlsx":
            return _xlsx(path, settings)
        if suffix == ".pptx":
            return _pptx(path, settings)
        if kind == "image":
            return _image(path, suffix)
        if kind == "audio":
            return _audio(path, suffix)
        if kind == "video":
            return _video(path, suffix)
    except Exception as e:  # noqa: BLE001 解析失败必须降级，不影响文件可用性
        return ParseResult(
            text=None,
            meta={"error": f"{type(e).__name__}: {e}", "format": suffix},
            parse_status="metadata_only",
        )
    return ParseResult(
        text=None, meta={"format": suffix}, parse_status="metadata_only"
    )


def _truncate(text: str, settings) -> tuple[str, bool]:
    limit = int(getattr(settings, "attachment_text_chars_per_file", 12000))
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _doc_result(text: str, meta: dict, settings, *, has_text: bool = True) -> ParseResult:
    text, truncated = _truncate(text, settings)
    if truncated:
        meta["truncated"] = True
    return ParseResult(
        text=text if has_text and text.strip() else None,
        meta=meta,
        parse_status="ok" if has_text and text.strip() else "metadata_only",
    )


def _pdf(path: pathlib.Path, settings) -> ParseResult:
    try:
        from app.parsing.chunker import serialize_table
        from app.parsing.loader import load_document

        doc = load_document(path)
        parts: list[str] = []
        current_page = None
        for paragraph in doc.paragraphs:
            if not paragraph.text.strip():
                continue
            if paragraph.page and paragraph.page != current_page:
                parts.append(f"【第{paragraph.page}页】")
                current_page = paragraph.page
            parts.append(paragraph.text)
        for table in doc.tables:
            rendered = serialize_table(table)
            if rendered:
                prefix = f"【第{table.page}页】" if table.page else ""
                parts.append(f"{prefix}{rendered}")
        text = "\n\n".join(parts)
        meta = {"pages": doc.pages, "tables": len(doc.tables), "format": "pdf"}
        return _doc_result(text, meta, settings)
    except Exception:  # noqa: BLE001
        # pdfplumber 表格提取失败时退回纯文本，保证大文档仍可用
        import pymupdf

        doc = pymupdf.open(path)
        text = "\n".join(page.get_text("text") for page in doc)
        meta = {"pages": doc.page_count, "tables": 0, "format": "pdf"}
        return _doc_result(text, meta, settings)


def _read_text(path: pathlib.Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _markdown_or_text(path: pathlib.Path, settings) -> ParseResult:
    from app.parsing.chunker import serialize_table
    from app.parsing.loader import load_document

    doc = load_document(path)
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        rendered = serialize_table(table)
        if rendered:
            parts.append(rendered)
    text = "\n\n".join(parts)
    return _doc_result(
        text,
        {"format": extension_of(path.name).lstrip("."), "tables": len(doc.tables)},
        settings,
    )


def _docx(path: pathlib.Path, settings) -> ParseResult:
    from docx import Document

    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables[:80]:
        for row in table.rows[:200]:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    meta = {
        "format": "docx",
        "paragraphs": len(doc.paragraphs),
        "tables": len(doc.tables),
    }
    return _doc_result("\n".join(parts), meta, settings)


def _csv(path: pathlib.Path, settings) -> ParseResult:
    text = _read_text(path)
    rows = 0
    lines: list[str] = []
    try:
        reader = csv.reader(io.StringIO(text))
        for index, row in enumerate(reader):
            if index >= 1000:
                break
            lines.append(" | ".join(cell.strip() for cell in row if cell.strip()))
            rows += 1
    except csv.Error:
        lines = text.splitlines()[:1000]
        rows = len(lines)
    meta = {"format": "csv", "rows": rows}
    return _doc_result("\n".join(lines), meta, settings)


def _xlsx(path: pathlib.Path, settings) -> ParseResult:
    import openpyxl

    workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        lines: list[str] = []
        for sheet_name in workbook.sheetnames[:10]:
            sheet = workbook[sheet_name]
            lines.append(f"【工作表 {sheet_name}】")
            for index, row in enumerate(sheet.iter_rows(values_only=True)):
                if index >= 200:
                    lines.append("…（行数超限已截断）")
                    break
                cells = [str(value).strip() for value in row if value is not None and str(value).strip()]
                if cells:
                    lines.append(" | ".join(cells))
        meta = {"format": "xlsx", "sheets": len(workbook.sheetnames)}
        return _doc_result("\n".join(lines), meta, settings)
    finally:
        workbook.close()


def _pptx(path: pathlib.Path, settings) -> ParseResult:
    from pptx import Presentation

    presentation = Presentation(str(path))
    lines: list[str] = []
    # python-pptx 的 Slides 不支持切片（切片会走 __getitem__ 并抛 AttributeError）
    for slide_index, slide in enumerate(list(presentation.slides)[:100], start=1):
        texts = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.text_frame:
                value = shape.text_frame.text.strip()
                if value:
                    texts.append(value)
        if texts:
            lines.append(f"【第 {slide_index} 页】")
            lines.extend(texts)
    meta = {"format": "pptx", "slides": len(presentation.slides)}
    return _doc_result("\n".join(lines), meta, settings)


def _image(path: pathlib.Path, suffix: str) -> ParseResult:
    if suffix == ".svg":
        raw = path.read_text(encoding="utf-8", errors="replace")[:4096]
        width = _svg_dimension(raw, "width")
        height = _svg_dimension(raw, "height")
        return ParseResult(
            text=None,
            meta={"format": "svg", "width": width, "height": height},
            parse_status="ok",
        )
    from PIL import Image

    with Image.open(path) as image:
        width, height = image.size
        meta = {
            "format": (image.format or suffix.lstrip(".")).lower(),
            "width": width,
            "height": height,
            "mode": image.mode,
        }
        try:
            orientation = image.getexif().get(274)
            if orientation:
                meta["orientation"] = orientation
        except Exception:  # noqa: BLE001, S110 EXIF 缺失不应影响上传
            pass
    return ParseResult(text=None, meta=meta, parse_status="ok")


def _svg_dimension(raw: str, attr: str) -> int | None:
    match = re.search(rf"\b{attr}\s*=\s*[\"']([0-9.]+)", raw, re.IGNORECASE)
    if not match:
        return None
    try:
        return int(float(match.group(1)))
    except ValueError:
        return None


def _audio(path: pathlib.Path, suffix: str) -> ParseResult:
    meta = {"format": suffix.lstrip(".")}
    try:
        import mutagen

        audio = mutagen.File(str(path))
        if audio is not None:
            info = getattr(audio, "info", None)
            if info is not None and getattr(info, "length", None):
                meta["duration"] = round(float(info.length), 2)
            if info is not None and getattr(info, "bitrate", None):
                meta["bitrate"] = int(info.bitrate)
            tags = getattr(audio, "tags", None) or {}
            for key, label in (("title", "title"), ("artist", "artist")):
                value = tags.get(key) if hasattr(tags, "get") else None
                if value:
                    meta[label] = str(value)
    except Exception:  # noqa: BLE001, S110 音频元数据是增强信息
        pass
    return ParseResult(text=None, meta=meta, parse_status="ok")


def _video(path: pathlib.Path, suffix: str) -> ParseResult:
    return ParseResult(
        text=None,
        meta={"format": suffix.lstrip("."), "size": path.stat().st_size},
        parse_status="ok",
    )
