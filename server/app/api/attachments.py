"""会话附件上传接口：分片、续传、完成、内容读取与删除。"""

import pathlib
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.attachments import repo, storage
from app.core.config import get_settings
from app.core.exceptions import ParamError
from app.session import store as session_store

router = APIRouter(prefix="/api")


class UploadInitRequest(BaseModel):
    session_id: str | None = Field(default=None, alias="sessionId", max_length=64)
    filename: str = Field(min_length=1, max_length=300)
    size: int = Field(ge=1)
    mime: str | None = Field(default=None, max_length=160)

    model_config = {"populate_by_name": True}


class UploadCompleteRequest(BaseModel):
    session_id: str = Field(alias="sessionId", max_length=64)

    model_config = {"populate_by_name": True}


def _require_session(session_id: str | None) -> str:
    if not session_store.valid_id(session_id):
        raise ParamError("sessionId 格式不合法")
    return session_id


@router.post("/uploads/init")
async def init_upload(body: UploadInitRequest) -> dict:
    settings = get_settings()
    if body.session_id and not session_store.valid_id(body.session_id):
        raise ParamError("sessionId 格式不合法")
    data = storage.init_upload(
        settings,
        session_id=body.session_id,
        filename=body.filename,
        size=body.size,
        client_mime=body.mime,
    )
    return {"data": data}


@router.put("/uploads/{upload_id}/chunks/{index}")
async def upload_chunk(
    upload_id: str,
    index: int,
    request: Request,
    session_id: str = Query(alias="sessionId"),
) -> dict:
    session_id = _require_session(session_id)
    data = await request.body()
    result = await run_in_threadpool(
        storage.save_chunk, get_settings(), upload_id, index, data, session_id
    )
    return {"data": result}


@router.get("/uploads/{upload_id}")
async def upload_status(
    upload_id: str,
    session_id: str = Query(alias="sessionId"),
) -> dict:
    session_id = _require_session(session_id)
    return {"data": storage.upload_status(get_settings(), upload_id, session_id)}


@router.post("/uploads/{upload_id}/complete")
async def complete_upload(upload_id: str, body: UploadCompleteRequest) -> dict:
    session_id = _require_session(body.session_id)
    data = await run_in_threadpool(
        storage.complete_and_parse, get_settings(), upload_id, session_id
    )
    return {"data": data}


@router.delete("/uploads/{upload_id}")
async def abort_upload(
    upload_id: str,
    session_id: str = Query(alias="sessionId"),
) -> dict:
    session_id = _require_session(session_id)
    storage.delete_upload(get_settings(), upload_id, session_id)
    return {"data": True}


@router.get("/attachments/{attachment_id}/content")
async def attachment_content(
    attachment_id: str,
    session_id: str = Query(alias="sessionId"),
    download: bool = False,
) -> FileResponse:
    session_id = _require_session(session_id)
    record, path = storage.get_content(get_settings(), attachment_id, session_id)
    suffix = pathlib.Path(record["filename"]).suffix.lower()
    disposition = "attachment" if download or (record["kind"] == "document" and suffix != ".pdf") else "inline"
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(record['filename'])}",
    }
    if record["mime"] == "image/svg+xml":
        headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
    return FileResponse(path, media_type=record["mime"], headers=headers)


@router.get("/sessions/{session_id}/attachments")
async def list_session_attachments(session_id: str) -> dict:
    session_id = _require_session(session_id)
    settings = get_settings()
    if session_store.get_session(settings.session_db, session_id) is None:
        return {"data": []}
    records = repo.list_attachments(settings.session_db, session_id)
    return {"data": [repo.public_attachment(record) for record in records]}


@router.delete("/attachments/{attachment_id}")
async def delete_attachment(
    attachment_id: str,
    session_id: str = Query(alias="sessionId"),
) -> dict:
    session_id = _require_session(session_id)
    storage.delete_attachment(get_settings(), attachment_id, session_id)
    return {"data": True}
