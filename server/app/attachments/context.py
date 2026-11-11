"""把会话附件转换成检索/生成可用的上下文。

文档文本按语义边界切块，按问题相关度排序；图片/音视频生成元数据块。
输出统一为 retrieval.searcher.Hit，复用现有上下文组装、引用与置信度链路。
"""

import logging
import re

from app.retrieval.searcher import Hit

try:
    import jieba

    jieba.setLogLevel(logging.WARNING)
except Exception:  # noqa: BLE001
    jieba = None

_SENT = re.compile(r"(?<=[。！？；!?;])\s*")
_TOKEN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9._-]{1,}")


def _tokens(text: str) -> set[str]:
    if jieba is not None:
        return {
            token.strip().lower()
            for token in jieba.lcut_for_search(text or "")
            if len(token.strip()) >= 2
        }
    return {token.lower() for token in _TOKEN.findall(text or "") if len(token) >= 2}


def _split_text(text: str, target: int = 800) -> list[str]:
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    # PDF 文本带【第N页】标记：按页切段，保证每个 chunk 的 page 可精确回溯
    segments = [segment.strip() for segment in re.split(r"(?=【第\d+页】)", text) if segment.strip()]
    chunks: list[str] = []
    for segment in segments:
        marker = ""
        match = _PAGE_MARK.match(segment)
        if match:
            marker = match.group(0)
            segment = segment[match.end() :].lstrip()
        if not segment:
            continue

        def emit(value: str, marker: str = marker) -> None:
            value = value.strip()
            if value:
                chunks.append(f"{marker}{value}" if marker else value)

        pieces = [piece.strip() for piece in _SENT.split(segment) if piece.strip()]
        buf = ""
        for piece in pieces:
            if len(piece) > target * 1.5:
                if buf:
                    emit(buf)
                    buf = ""
                for index in range(0, len(piece), target):
                    emit(piece[index : index + target])
                continue
            if buf and len(buf) + len(piece) > target:
                emit(buf)
                buf = piece
            else:
                buf += piece
        if buf:
            emit(buf)
    return chunks


def _score(question_tokens: set[str], text: str, filename_tokens: set[str], index: int) -> float:
    if not question_tokens:
        return 0.1 / (index + 1)
    text_tokens = _tokens(text)
    overlap = len(question_tokens & text_tokens)
    score = overlap / max(1, len(question_tokens))
    if filename_tokens & question_tokens:
        score += 0.15
    return score + 0.02 / (index + 1)


def format_size(size: int | None) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def format_duration(seconds: float | None) -> str:
    if not seconds:
        return "时长未知"
    total = round(float(seconds))
    minutes, sec = divmod(total, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}小时{minutes}分{sec}秒"
    return f"{minutes}分{sec}秒"


def metadata_text(record: dict, settings) -> str:
    filename = record["filename"]
    meta = record.get("meta") or {}
    fmt = str(meta.get("format") or "").upper()
    size = format_size(record.get("size"))
    kind = record.get("kind")
    if kind == "image":
        width, height = meta.get("width"), meta.get("height")
        dimensions = f"{width}×{height}" if width and height else "尺寸未知"
        note = (
            "已启用视觉理解，可直接参考图片内容。"
            if getattr(settings, "attachment_vision", False)
            else "当前未启用视觉理解，仅提供文件元数据。"
        )
        return f"图片附件：{filename}（{fmt or '图片'}，{dimensions}，{size}）。{note}"
    if kind == "audio":
        duration = format_duration(meta.get("duration"))
        return f"音频附件：{filename}（{fmt or '音频'}，{duration}，{size}）。当前未做语音转写。"
    if kind == "video":
        return f"视频附件：{filename}（{fmt or '视频'}，{size}）。当前未做视频内容理解。"
    return f"文档附件：{filename}（{fmt or '文档'}，{size}）。未能提取文本，仅可预览/下载。"


_PAGE_MARK = re.compile(r"^【第(\d+)页】")


def _page_of(text: str) -> int | None:
    match = _PAGE_MARK.match((text or "").lstrip())
    return int(match.group(1)) if match else None


def build_hits(records: list[dict], question: str, settings) -> list[Hit]:
    if not records:
        return []
    question_tokens = _tokens(question)
    candidates: list[tuple[float, int, dict, str, int | None]] = []
    order = 0
    per_file = int(getattr(settings, "attachment_hits_per_file", 2))
    for record in records:
        text = record.get("text") or ""
        if record.get("kind") == "document" and text.strip():
            filename_tokens = _tokens(record["filename"])
            chunks = _split_text(text)
            scored = [
                (_score(question_tokens, chunk, filename_tokens, index), index, chunk)
                for index, chunk in enumerate(chunks)
            ]
            scored.sort(key=lambda item: item[0], reverse=True)
            selected = scored[:per_file]
            if not any(item[0] > 0 for item in selected):
                selected = scored[: min(per_file, len(scored))]
            for score, _index, chunk in selected:
                candidates.append((score, order, record, chunk, _page_of(chunk)))
                order += 1
        else:
            candidates.append(
                (0.45, order, record, metadata_text(record, settings), None)
            )
            order += 1

    candidates.sort(key=lambda item: item[0], reverse=True)
    limit = int(getattr(settings, "attachment_max_hits", 8))
    hits: list[Hit] = []
    for rank, (_score_value, _order, record, text, page) in enumerate(candidates[:limit]):
        hits.append(
            Hit(
                chunk_id=f"attachment:{record['id']}:{rank}",
                rrf=1.0 / (rank + 1),
                sim=None,
                text=text,
                doc_id=f"attachment:{record['id']}",
                doc_name=record["filename"],
                section="会话附件",
                page=page,
                kind="attachment",
                table_no=None,
                parent_text=text,
                folder="",
                attachment_id=record["id"],
            )
        )
    return hits


def build_prompt_block(records: list[dict], settings) -> str:
    if not records:
        return ""
    total_limit = int(getattr(settings, "attachment_text_chars_total", 30000))
    parts = ["【会话附件】"]
    used = 0
    for index, record in enumerate(records, start=1):
        header = (
            f"[附件{index}] {record['filename']}"
            f"（{record.get('kind') or '文件'}，{format_size(record.get('size'))}）"
        )
        text = record.get("text") or ""
        if text.strip():
            remain = max(0, total_limit - used)
            if remain <= 0:
                parts.append(f"{header}\n（附件文本已达上下文上限，后续内容省略）")
                continue
            excerpt = text[:remain]
            used += len(excerpt)
            suffix = "\n（附件文本已截断）" if len(excerpt) < len(text) else ""
            parts.append(f"{header}\n{excerpt}{suffix}")
        else:
            parts.append(f"{header}\n{metadata_text(record, settings)}")
    return "\n\n".join(parts)


def image_records(records: list[dict]) -> list[dict]:
    return [record for record in records if record.get("kind") == "image"]
