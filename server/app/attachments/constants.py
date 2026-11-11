"""附件类型、扩展名、MIME 与磁盘命名约定。"""

import pathlib
import re

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
DOCUMENT_EXTS = {".pdf", ".docx", ".md", ".markdown", ".txt", ".csv", ".xlsx", ".pptx"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
VIDEO_EXTS = {".mp4", ".webm", ".mov"}

KIND_BY_EXT: dict[str, str] = {
    **{ext: "image" for ext in IMAGE_EXTS},
    **{ext: "document" for ext in DOCUMENT_EXTS},
    **{ext: "audio" for ext in AUDIO_EXTS},
    **{ext: "video" for ext in VIDEO_EXTS},
}
ALLOWED_EXTS = set(KIND_BY_EXT)

MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}

_SAFE_NAME = re.compile(r"[^\w.\-\u4e00-\u9fff]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def extension_of(filename: str) -> str:
    return pathlib.Path(filename or "").suffix.lower()


def kind_of(filename: str) -> str | None:
    return KIND_BY_EXT.get(extension_of(filename))


def is_allowed(filename: str) -> bool:
    return extension_of(filename) in ALLOWED_EXTS


def mime_of(filename: str, client_mime: str | None = None) -> str:
    """优先使用扩展名映射，客户端 MIME 仅作缺失时兜底。"""
    return MIME_BY_EXT.get(extension_of(filename)) or (client_mime or "application/octet-stream")


def display_name(filename: str) -> str:
    """只保留文件名本身，去掉路径与控制字符，限制长度。"""
    name = pathlib.Path(filename or "").name
    name = _CONTROL.sub("", name).strip()
    name = _SAFE_NAME.sub("_", name)
    return (name or "unnamed")[:200]


def stored_name(attachment_id: str, suffix: str) -> str:
    """磁盘名只由服务端 ID 与扩展名构成，不拼接客户端文件名。"""
    return f"{attachment_id}{suffix}"
