"""知识库管理接口：入库（幂等）、文档列表、删除。"""

import pathlib
import re
from uuid import uuid4

from fastapi import APIRouter, UploadFile
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.exceptions import (
    DocumentNotFoundError,
    FileTooLargeError,
    UnsupportedFormatError,
)
from app.indexing import store
from app.indexing.ingest import ALLOWED_EXTS, ingest_file
from app.indexing.manifest import Manifest

router = APIRouter(prefix="/api/kb")

MAX_BYTES = 20 * 1024 * 1024
_SAFE_NAME = re.compile(r"[^\w.\-\u4e00-\u9fff]+")


@router.post("/ingest")
async def ingest(file: UploadFile) -> dict:
    settings = get_settings()
    filename = pathlib.Path(file.filename or "unnamed").name
    suffix = pathlib.Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTS:
        raise UnsupportedFormatError(
            f"仅支持 {'/'.join(sorted(ALLOWED_EXTS))}，收到 {suffix or '无扩展名'}"
        )
    data = await file.read()
    if len(data) > MAX_BYTES:
        raise FileTooLargeError("文件超过 20MB 上限")

    safe = _SAFE_NAME.sub("_", filename)
    stored_name = f"{uuid4().hex[:8]}_{safe}"
    target_dir = pathlib.Path(settings.upload_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / stored_name
    path.write_bytes(data)
    try:
        result = await run_in_threadpool(ingest_file, path, None, stored_name)
    except Exception:
        path.unlink(missing_ok=True)  # 入库失败不留孤儿上传文件
        raise
    if result.get("duplicated") or result.get("chunks", 0) == 0:
        path.unlink(missing_ok=True)
    return {"data": result}


@router.post("/preview")
async def preview(file: UploadFile) -> dict:
    """入库预检：解析演练与质量信号，不写入存储。"""
    filename = pathlib.Path(file.filename or "unnamed").name
    suffix = pathlib.Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTS:
        raise UnsupportedFormatError(
            f"仅支持 {'/'.join(sorted(ALLOWED_EXTS))}，收到 {suffix or '无扩展名'}"
        )
    data = await file.read()
    if len(data) > MAX_BYTES:
        raise FileTooLargeError("文件超过 20MB 上限")
    safe = _SAFE_NAME.sub("_", filename)
    target_dir = pathlib.Path(get_settings().upload_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"preview_{uuid4().hex[:8]}_{safe}"
    path.write_bytes(data)
    try:
        from app.indexing.preview import preview_document

        report = await run_in_threadpool(preview_document, path)
    finally:
        path.unlink(missing_ok=True)
    report["filename"] = filename  # 临时文件前缀不应出现在用户看到的预检报告里
    return {"data": report}


@router.get("/documents")
async def documents() -> dict:
    settings = get_settings()
    manifest = Manifest(pathlib.Path(settings.chroma_dir) / "manifest.json")
    docs = [
        {
            "docId": doc_id,
            "filename": info["filename"],
            "chunks": info.get("chunks", 0),
            "ingestedAt": info.get("ingestedAt"),
        }
        for doc_id, info in manifest.documents().items()
    ]
    docs.sort(key=lambda d: d["ingestedAt"] or "", reverse=True)
    return {"data": docs}


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: str) -> dict:
    settings = get_settings()
    manifest = Manifest(pathlib.Path(settings.chroma_dir) / "manifest.json")
    info = manifest.get(doc_id)
    if not info:
        raise DocumentNotFoundError(f"文档不存在: {doc_id}")
    store.delete_doc(settings, doc_id)
    manifest.remove(doc_id)
    manifest.save()
    stored = info.get("stored_name")
    if stored:
        (pathlib.Path(settings.upload_dir) / stored).unlink(missing_ok=True)
    return {"data": True}
