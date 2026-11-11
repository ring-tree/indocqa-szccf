"""附件分片存储：staging 目录、合并、magic 校验、内容读取与清理。"""

import math
import pathlib
import shutil
import time
from uuid import uuid4

from app.attachments import parsers, repo
from app.attachments.constants import (
    ALLOWED_EXTS,
    display_name,
    extension_of,
    kind_of,
    mime_of,
    stored_name,
)
from app.core.exceptions import (
    AttachmentLimitError,
    AttachmentNotFoundError,
    AttachmentStorageError,
    ChunkError,
    FileTooLargeError,
    ParamError,
    PermissionDeniedError,
    UnsupportedFormatError,
    UploadConflictError,
    UploadNotFoundError,
)
from app.session import store as session_store

_TEXT_EXTS = {".md", ".markdown", ".txt", ".csv"}


def _now_plus(hours: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + hours * 3600))


def _root(settings) -> pathlib.Path:
    return pathlib.Path(settings.attachment_dir)


def _staging_dir(settings, upload_id: str) -> pathlib.Path:
    return _root(settings) / ".staging" / upload_id


def _final_dir(settings, session_id: str) -> pathlib.Path:
    return _root(settings) / session_id


def _chunk_path(settings, upload_id: str, index: int) -> pathlib.Path:
    return _staging_dir(settings, upload_id) / f"chunk_{index:06d}.part"


def _max_label(settings) -> str:
    return f"{int(settings.attachment_max_bytes) // (1024 * 1024)}MB"


def init_upload(
    settings,
    *,
    session_id: str | None,
    filename: str,
    size: int,
    client_mime: str | None = None,
) -> dict:
    filename = display_name(filename)
    suffix = extension_of(filename)
    kind = kind_of(filename)
    if suffix not in ALLOWED_EXTS or kind is None:
        raise UnsupportedFormatError(
            f"不支持的附件格式 {suffix or '（无扩展名）'}，"
            f"支持：{'/'.join(sorted(ALLOWED_EXTS))}"
        )
    if size <= 0:
        raise ParamError("文件大小必须大于 0")
    if size > int(settings.attachment_max_bytes):
        raise FileTooLargeError(f"文件超过 {_max_label(settings)} 上限")
    session = session_store.ensure_session(settings.session_db, session_id, filename)
    sid = session["id"]
    if repo.count_uploading(settings.session_db, sid) >= int(settings.attachment_max_files):
        raise AttachmentLimitError(
            f"同一会话同时上传的文件不能超过 {settings.attachment_max_files} 个"
        )

    upload_id = uuid4().hex
    chunk_size = int(settings.attachment_chunk_bytes)
    total_chunks = max(1, math.ceil(size / chunk_size))
    staging = _staging_dir(settings, upload_id)
    staging.mkdir(parents=True, exist_ok=True)
    repo.create_upload(
        settings.session_db,
        upload_id=upload_id,
        session_id=sid,
        filename=filename,
        mime=mime_of(filename, client_mime),
        kind=kind,
        size=int(size),
        chunk_size=chunk_size,
        total_chunks=total_chunks,
        expires_at=_now_plus(int(settings.attachment_staging_ttl_hours)),
    )
    return {
        "uploadId": upload_id,
        "sessionId": sid,
        "chunkSize": chunk_size,
        "totalChunks": total_chunks,
        "received": [],
        "expiresAt": _now_plus(int(settings.attachment_staging_ttl_hours)),
    }


def _require_upload(settings, upload_id: str, session_id: str) -> dict:
    row = repo.get_upload(settings.session_db, upload_id)
    if row is None:
        raise UploadNotFoundError("上传任务不存在或已过期")
    if row["session_id"] != session_id:
        raise PermissionDeniedError("无权访问该上传任务")
    return row


def upload_status(settings, upload_id: str, session_id: str) -> dict:
    row = _require_upload(settings, upload_id, session_id)
    received: list[int] = []
    uploaded = 0
    staging = _staging_dir(settings, upload_id)
    if staging.exists():
        for item in staging.glob("chunk_*.part"):
            try:
                index = int(item.stem.split("_", 1)[1])
            except (IndexError, ValueError):
                continue
            received.append(index)
            uploaded += item.stat().st_size
    received.sort()
    total = int(row["total_chunks"])
    size = int(row["size"])
    return {
        "uploadId": upload_id,
        "status": row["status"],
        "received": received,
        "totalChunks": total,
        "uploadedBytes": uploaded,
        "percent": round(100 * uploaded / size) if size else 0,
        "chunkSize": int(row["chunk_size"]),
        "expiresAt": row["expires_at"],
    }


def save_chunk(settings, upload_id: str, index: int, data: bytes, session_id: str) -> dict:
    row = _require_upload(settings, upload_id, session_id)
    if row["status"] == "completing":
        raise UploadConflictError("上传任务正在合并，请稍后重试")
    if row["status"] != "uploading":
        raise UploadNotFoundError("上传任务已结束，请重新上传")
    total = int(row["total_chunks"])
    chunk_size = int(row["chunk_size"])
    if index < 0 or index >= total:
        raise ChunkError(f"分片序号越界：{index}，应在 0..{total - 1}")
    if not data:
        raise ChunkError("分片内容为空")
    if len(data) > chunk_size:
        raise ChunkError(f"分片超过 {chunk_size} 字节")
    staging = _staging_dir(settings, upload_id)
    staging.mkdir(parents=True, exist_ok=True)
    target = _chunk_path(settings, upload_id, index)
    temp = target.with_suffix(".tmp")
    temp.write_bytes(data)
    temp.replace(target)
    return upload_status(settings, upload_id, session_id)


def _magic_ok(path: pathlib.Path, suffix: str) -> bool:
    if suffix in _TEXT_EXTS:
        return True
    try:
        with path.open("rb") as f:
            head = f.read(4096)
    except OSError as e:
        raise AttachmentStorageError("附件读取失败", detail=str(e)) from e
    if suffix == ".pdf":
        return head.startswith(b"%PDF-")
    if suffix == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return head.startswith(b"\xff\xd8\xff")
    if suffix == ".gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    if suffix == ".webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if suffix == ".bmp":
        return head.startswith(b"BM")
    if suffix == ".svg":
        lower = head.lower()
        return b"<svg" in lower[:2048] or (b"<?xml" in lower[:512] and b"<svg" in lower)
    if suffix in {".docx", ".xlsx", ".pptx"}:
        return head.startswith(b"PK\x03\x04")
    if suffix == ".mp3":
        return head.startswith(b"ID3") or (
            len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0
        )
    if suffix == ".wav":
        return head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if suffix == ".ogg":
        return head.startswith(b"OggS")
    if suffix == ".flac":
        return head.startswith(b"fLaC")
    if suffix in {".m4a", ".mp4", ".mov"}:
        return len(head) >= 8 and head[4:8] == b"ftyp"
    if suffix == ".webm":
        return head.startswith(b"\x1aE\xdf\xa3")
    if suffix == ".aac":
        return head[:2] in {b"\xff\xf1", b"\xff\xf9"} or (
            len(head) >= 8 and head[4:8] == b"ftyp"
        )
    return True


def _safe_path(root: pathlib.Path, path: pathlib.Path) -> pathlib.Path:
    root = root.resolve()
    path = path.resolve()
    if not path.is_relative_to(root):
        raise AttachmentStorageError("附件路径越界")
    return path


def complete_and_parse(settings, upload_id: str, session_id: str) -> dict:
    row = _require_upload(settings, upload_id, session_id)
    if row["status"] not in {"uploading", "failed"}:
        raise UploadConflictError("上传任务已完成或正在合并")
    status = upload_status(settings, upload_id, session_id)
    total = int(row["total_chunks"])
    missing = [index for index in range(total) if index not in set(status["received"])]
    if missing:
        raise ChunkError(
            "分片不完整，请续传",
            detail="缺少分片：" + ",".join(str(i) for i in missing[:20]),
        )

    suffix = extension_of(row["filename"])
    final_dir = _final_dir(settings, session_id)
    final_dir.mkdir(parents=True, exist_ok=True)
    disk_name = stored_name(upload_id, suffix)
    final_path = _safe_path(_root(settings), final_dir / disk_name)
    repo.set_upload_status(settings.session_db, upload_id, "completing")
    merged = 0
    try:
        with final_path.open("wb") as out:
            for index in range(total):
                chunk = _chunk_path(settings, upload_id, index)
                with chunk.open("rb") as src:
                    while True:
                        buf = src.read(1024 * 1024)
                        if not buf:
                            break
                        out.write(buf)
                        merged += len(buf)
        if merged != int(row["size"]):
            raise ChunkError(
                "文件实际大小与声明不一致",
                detail=f"声明 {row['size']} 字节，实际 {merged} 字节",
            )
        if not _magic_ok(final_path, suffix):
            final_path.unlink(missing_ok=True)
            repo.delete_upload(settings.session_db, upload_id)
            shutil.rmtree(_staging_dir(settings, upload_id), ignore_errors=True)
            raise UnsupportedFormatError("文件内容与扩展名不匹配")
        result = parsers.extract(final_path, settings)
        try:
            record = repo.add_attachment(
                settings.session_db,
                attachment_id=upload_id,
                session_id=session_id,
                filename=row["filename"],
                stored_name=disk_name,
                mime=row["mime"],
                kind=row["kind"],
                size=merged,
                text=result.text,
                meta=result.meta,
                parse_status=result.parse_status,
            )
        except Exception as e:
            final_path.unlink(missing_ok=True)
            raise AttachmentStorageError("附件记录写入失败", detail=str(e)) from e
        repo.delete_upload(settings.session_db, upload_id)
        shutil.rmtree(_staging_dir(settings, upload_id), ignore_errors=True)
        return repo.public_attachment(record)
    except (ChunkError, UnsupportedFormatError, AttachmentStorageError):
        repo.set_upload_status(settings.session_db, upload_id, "failed")
        raise
    except Exception as e:
        repo.set_upload_status(settings.session_db, upload_id, "failed")
        final_path.unlink(missing_ok=True)
        raise AttachmentStorageError("附件合并失败", detail=str(e)) from e


def get_content(settings, attachment_id: str, session_id: str) -> tuple[dict, pathlib.Path]:
    record = repo.get_attachment(settings.session_db, attachment_id)
    if record is None:
        raise AttachmentNotFoundError("附件不存在或已删除")
    if record["session_id"] != session_id:
        raise PermissionDeniedError("无权访问该附件")
    path = _safe_path(_root(settings), _final_dir(settings, session_id) / record["stored_name"])
    if not path.exists():
        raise AttachmentNotFoundError("附件文件不存在")
    return record, path


def delete_attachment(settings, attachment_id: str, session_id: str) -> None:
    record = repo.get_attachment(settings.session_db, attachment_id)
    if record is None:
        raise AttachmentNotFoundError("附件不存在或已删除")
    if record["session_id"] != session_id:
        raise PermissionDeniedError("无权删除该附件")
    path = _safe_path(_root(settings), _final_dir(settings, session_id) / record["stored_name"])
    path.unlink(missing_ok=True)
    repo.delete_attachment(settings.session_db, attachment_id)


def delete_upload(settings, upload_id: str, session_id: str) -> None:
    row = repo.get_upload(settings.session_db, upload_id)
    if row is None:
        raise UploadNotFoundError("上传任务不存在或已过期")
    if row["session_id"] != session_id:
        raise PermissionDeniedError("无权删除该上传任务")
    shutil.rmtree(_staging_dir(settings, upload_id), ignore_errors=True)
    repo.delete_upload(settings.session_db, upload_id)


def delete_session_files(settings, session_id: str) -> int:
    if not session_store.valid_id(session_id):
        return 0
    removed = 0
    for record in repo.session_attachment_records(settings.session_db, session_id):
        path = _final_dir(settings, session_id) / record["stored_name"]
        if path.exists():
            path.unlink(missing_ok=True)
            removed += 1
    for row in repo.session_upload_records(settings.session_db, session_id):
        staging = _staging_dir(settings, row["id"])
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
            removed += 1
    session_dir = _final_dir(settings, session_id)
    if session_dir.exists() and not any(session_dir.iterdir()):
        session_dir.rmdir()
    return removed


def cleanup_staging(settings) -> int:
    removed = 0
    root = _root(settings) / ".staging"
    cutoff = time.time() - int(settings.attachment_staging_ttl_hours) * 3600
    if root.exists():
        for item in root.iterdir():
            if not item.is_dir():
                continue
            if item.stat().st_mtime < cutoff:
                shutil.rmtree(item, ignore_errors=True)
                repo.delete_upload(settings.session_db, item.name)
                removed += 1
    for upload_id in repo.expired_upload_ids(settings.session_db):
        staging = _staging_dir(settings, upload_id)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        repo.delete_upload(settings.session_db, upload_id)
        removed += 1
    return removed


def cleanup_orphans(settings) -> int:
    root = _root(settings)
    if not root.exists():
        return 0
    known = repo.all_stored_names(settings.session_db)
    removed = 0
    for session_dir in root.iterdir():
        if not session_dir.is_dir() or session_dir.name == ".staging":
            continue
        for item in session_dir.iterdir():
            if item.is_file() and item.name not in known:
                item.unlink(missing_ok=True)
                removed += 1
        if not any(session_dir.iterdir()):
            session_dir.rmdir()
    return removed
