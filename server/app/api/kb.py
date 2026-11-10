"""知识库管理接口：目录树、入库（幂等）、文档列表/移动/删除。"""

import pathlib
import re
import threading
from uuid import uuid4

from fastapi import APIRouter, Form, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.exceptions import (
    AIUnavailableError,
    DocumentNotFoundError,
    FileTooLargeError,
    ParamError,
    UnsupportedFormatError,
)
from app.indexing import store
from app.indexing.folders import (
    DEFAULT_FOLDER,
    FolderPathError,
    ancestors,
    folder_counts,
    is_within,
    normalize_folder,
    parent_of,
)
from app.indexing.ingest import ALLOWED_EXTS, ingest_file
from app.indexing.jobs import create_job, get_job, run_ingest_job
from app.indexing.lock import write_guard
from app.indexing.manifest import Manifest
from app.indexing.moves import batch_move, undo_move

router = APIRouter(prefix="/api/kb")

MAX_BYTES = 20 * 1024 * 1024
_SAFE_NAME = re.compile(r"[^\w.\-\u4e00-\u9fff]+")


class FolderCreate(BaseModel):
    path: str = Field(min_length=1, max_length=200)


class FolderRename(BaseModel):
    source: str = Field(alias="from", min_length=1, max_length=200)
    target: str = Field(alias="to", min_length=1, max_length=200)

    model_config = {"populate_by_name": True}


class DocumentMove(BaseModel):
    folder: str = Field(min_length=1, max_length=200)


class BatchMoveRequest(BaseModel):
    doc_ids: list[str] = Field(min_length=1, max_length=200, alias="docIds")
    folder: str = Field(min_length=1, max_length=200)

    model_config = {"populate_by_name": True}


class ClassifyRequest(BaseModel):
    doc_id: str | None = Field(default=None, max_length=64, alias="docId")
    filename: str | None = Field(default=None, max_length=300)
    text: str | None = Field(default=None, max_length=4000)
    profile: str | None = Field(default=None, max_length=32)

    model_config = {"populate_by_name": True}


class ClassifyFeedback(BaseModel):
    doc_id: str | None = Field(default=None, alias="docId")
    suggested: str | None = None
    chosen: str | None = None
    action: str = Field(pattern="^(accept|adjust|reject)$")
    source: str | None = None

    model_config = {"populate_by_name": True}


def _folder_or_400(value: str | None, *, default: str | None = DEFAULT_FOLDER) -> str:
    try:
        return normalize_folder(value, default=default)
    except FolderPathError as e:
        raise ParamError(str(e)) from e


def _manifest(settings) -> Manifest:
    return Manifest(Manifest.path_for(settings))


def _folder_item(manifest: Manifest, path: str) -> dict:
    docs = manifest.documents()
    counts = folder_counts(docs)
    chunks = 0
    for info in docs.values():
        if is_within(info.get("folder") or DEFAULT_FOLDER, path):
            chunks += info.get("chunks", 0)
    return {
        "path": path,
        "name": path.split("/")[-1],
        "parent": parent_of(path),
        "documentCount": counts.get(path, 0),
        "chunkCount": chunks,
    }


@router.post("/ingest")
async def ingest(
    file: UploadFile,
    folder: str = Form(DEFAULT_FOLDER),
) -> dict:
    settings = get_settings()
    target_folder = _folder_or_400(folder)
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
        result = await run_in_threadpool(
            ingest_file, path, None, stored_name, target_folder, None, filename
        )
    except Exception:
        path.unlink(missing_ok=True)  # 入库失败不留孤儿上传文件
        raise
    if result.get("duplicated") or result.get("chunks", 0) == 0:
        path.unlink(missing_ok=True)
    return {"data": result}


@router.post("/ingest/jobs")
async def start_ingest_job(
    file: UploadFile,
    folder: str = Form(DEFAULT_FOLDER),
) -> dict:
    """启动带进度的后台入库任务；前端轮询 GET /api/kb/ingest/jobs/{jobId}。"""
    settings = get_settings()
    target_folder = _folder_or_400(folder)
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
    job_id = create_job(total_docs=1)
    threading.Thread(
        target=run_ingest_job,
        args=(job_id, path, settings, stored_name, target_folder, filename),
        daemon=True,
    ).start()
    return {"data": {"jobId": job_id, "totalDocs": 1, "folder": target_folder}}


@router.get("/ingest/jobs/{job_id}")
async def ingest_job_status(job_id: str) -> dict:
    job = get_job(job_id)
    if not job:
        raise DocumentNotFoundError(f"入库任务不存在或已过期: {job_id}")
    return {"data": job}


@router.post("/preview")
async def preview(file: UploadFile, classify: bool = False) -> dict:
    """入库预检：解析演练与质量信号，不写入存储；classify=1 时附带 AI 候选。"""
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

        report = await run_in_threadpool(preview_document, path, None, classify)
    finally:
        path.unlink(missing_ok=True)
    report["filename"] = filename  # 临时文件前缀不应出现在用户看到的预检报告里
    return {"data": report}


@router.get("/folders")
async def folders() -> dict:
    settings = get_settings()
    manifest = _manifest(settings)
    return {"data": [_folder_item(manifest, p) for p in manifest.folders()]}


@router.post("/folders")
async def create_folder(body: FolderCreate) -> dict:
    settings = get_settings()
    path = _folder_or_400(body.path, default=None)
    if not path:
        raise ParamError("目录路径不能为空")
    with write_guard(settings):
        manifest = _manifest(settings)
        for item in ancestors(path):
            manifest.ensure_folder(item)
        manifest.save()
        return {"data": _folder_item(manifest, path)}


@router.patch("/folders")
async def rename_folder(body: FolderRename) -> dict:
    settings = get_settings()
    source = _folder_or_400(body.source, default=None)
    target = _folder_or_400(body.target, default=None)
    if not source or not target:
        raise ParamError("目录路径不能为空")
    if target == source:
        return {"data": _folder_item(_manifest(settings), source)}
    if is_within(target, source):
        raise ParamError("不能把目录移动到自身的子目录下")
    with write_guard(settings):
        manifest = _manifest(settings)
        docs = manifest.docs_in_folder(source)
        if not docs and source not in manifest.folders():
            raise DocumentNotFoundError(f"目录不存在: {source}")
        for doc_id in docs:
            info = manifest.get(doc_id) or {}
            old = info.get("folder") or source
            suffix = old[len(source) :].strip("/")
            new_folder = f"{target}/{suffix}" if suffix else target
            store.update_doc_folder(settings, doc_id, new_folder)
            manifest.set_doc_folder(doc_id, new_folder)
        for path in list(manifest.folders()):
            if is_within(path, source):
                suffix = path[len(source) :].strip("/")
                new_path = f"{target}/{suffix}" if suffix else target
                manifest.remove_folder(path)
                if new_path:
                    manifest.ensure_folder(new_path)
        for item in ancestors(target):
            manifest.ensure_folder(item)
        manifest.save()
        return {"data": _folder_item(manifest, target)}


@router.delete("/folders")
async def delete_folder(path: str, mode: str = "move") -> dict:
    settings = get_settings()
    folder = _folder_or_400(path, default=None)
    if not folder:
        raise ParamError("目录路径不能为空")
    if folder == DEFAULT_FOLDER:
        raise ParamError(f"默认目录“{DEFAULT_FOLDER}”不能删除，可重命名")
    if mode not in {"move", "delete"}:
        raise ParamError("mode 仅支持 move 或 delete")
    with write_guard(settings):
        manifest = _manifest(settings)
        docs = manifest.docs_in_folder(folder)
        if not docs and folder not in manifest.folders():
            raise DocumentNotFoundError(f"目录不存在: {folder}")
        moved_or_deleted = 0
        if mode == "move":
            destination = parent_of(folder) or DEFAULT_FOLDER
            for doc_id in docs:
                store.update_doc_folder(settings, doc_id, destination)
                manifest.set_doc_folder(doc_id, destination)
                moved_or_deleted += 1
        else:
            for doc_id in docs:
                info = manifest.get(doc_id) or {}
                store.delete_doc(settings, doc_id)
                manifest.remove(doc_id)
                stored = info.get("stored_name")
                if stored:
                    (pathlib.Path(settings.upload_dir) / stored).unlink(missing_ok=True)
                moved_or_deleted += 1
        for item in list(manifest.folders()):
            if is_within(item, folder):
                manifest.remove_folder(item)
        manifest.save()
        return {"data": {"affected": moved_or_deleted, "mode": mode}}


@router.post("/documents/batch-move")
async def batch_move_documents(body: BatchMoveRequest) -> dict:
    settings = get_settings()
    target = _folder_or_400(body.folder)
    result = await run_in_threadpool(batch_move, settings, body.doc_ids, target)
    return {"data": result}


@router.post("/moves/{operation_id}/undo")
async def undo_move_operation(operation_id: str) -> dict:
    settings = get_settings()
    result = await run_in_threadpool(undo_move, settings, operation_id)
    return {"data": result}


@router.post("/documents/{doc_id}/move")
async def move_document(doc_id: str, body: DocumentMove) -> dict:
    settings = get_settings()
    target = _folder_or_400(body.folder)
    manifest = _manifest(settings)
    if not manifest.get(doc_id):
        raise DocumentNotFoundError(f"文档不存在: {doc_id}")
    result = await run_in_threadpool(batch_move, settings, [doc_id], target)
    if result["failed"]:
        raise DocumentNotFoundError(result["failed"][0].get("reason", "文档不存在"))
    return {
        "data": {
            "docId": doc_id,
            "folder": target,
            "operationId": result.get("operationId"),
            "skipped": bool(result.get("skipped")),
        }
    }


@router.post("/classify")
async def classify_document_route(body: ClassifyRequest) -> dict:
    """AI 分类建议；未配置输出层模型时返回 40904。"""
    settings = get_settings()
    from app.classification import service as classify_service

    if not classify_service.is_available(settings):
        raise AIUnavailableError("无可用模型")
    if body.doc_id:
        result = await classify_service.classify_document(settings, body.doc_id, body.profile)
    elif body.filename and body.text:
        result = await classify_service.classify_text(
            settings, body.filename, body.text, use_llm=True, profile=body.profile
        )
    else:
        raise ParamError("需要提供 docId，或 filename + text")
    return {"data": result}


@router.post("/classify/feedback")
async def classify_feedback(body: ClassifyFeedback) -> dict:
    settings = get_settings()
    from app.classification import service as classify_service

    classify_service.append_feedback(settings, body.model_dump())
    return {"data": True}


@router.get("/documents")
async def documents(folder: str | None = None) -> dict:
    settings = get_settings()
    manifest = _manifest(settings)
    scope = _folder_or_400(folder, default=None) if folder else None
    docs = [
        {
            "docId": doc_id,
            "filename": info["filename"],
            "folder": info.get("folder") or DEFAULT_FOLDER,
            "chunks": info.get("chunks", 0),
            "pages": info.get("pages"),
            "tables": info.get("tables"),
            "ingestedAt": info.get("ingestedAt"),
        }
        for doc_id, info in manifest.documents().items()
        if not scope or is_within(info.get("folder") or DEFAULT_FOLDER, scope)
    ]
    docs.sort(key=lambda d: d["ingestedAt"] or "", reverse=True)
    return {"data": docs}


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: str) -> dict:
    settings = get_settings()
    manifest = _manifest(settings)
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
