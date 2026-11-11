"""问答接口：SSE 流式（token / sources / done / error 事件见 openapi.yaml）。"""

import json
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api.schemas import ChatRequest
from app.attachments import repo as attachment_repo
from app.core.config import get_settings
from app.core.exceptions import (
    AttachmentLimitError,
    AttachmentNotFoundError,
    KBEmptyError,
    ParamError,
    UpstreamError,
)
from app.core.llm import LLMGateway
from app.graph import pipeline
from app.indexing.folders import FolderPathError, normalize_folder
from app.session import store as session_store

router = APIRouter(prefix="/api")


@router.post("/chat")
async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    settings = get_settings()
    rid = request.headers.get("X-Request-Id") or uuid4().hex[:12]

    if req.session_id and not session_store.valid_id(req.session_id):
        raise ParamError("sessionId 格式不合法")
    if req.attachment_ids and not req.session_id:
        raise ParamError("附件上传后必须携带 sessionId")
    attachments: list[dict] = []
    if req.attachment_ids:
        ids = list(dict.fromkeys(req.attachment_ids))
        if len(ids) > settings.attachment_max_files:
            raise AttachmentLimitError(f"单条消息附件不能超过 {settings.attachment_max_files} 个")
        attachments = attachment_repo.list_attachments(
            settings.session_db, req.session_id, ids=ids
        )
        if len(attachments) != len(ids):
            raise AttachmentNotFoundError("附件不存在或不属于当前会话")
    has_attachments = bool(attachments) or bool(
        req.session_id and attachment_repo.has_ready(settings.session_db, req.session_id)
    )
    if req.mode == "kb" and pipeline.kb_is_empty(settings) and not has_attachments:
        raise KBEmptyError("知识库为空，请先入库文档（POST /api/kb/ingest）")
    gateway = LLMGateway(settings, req.profile)
    if req.profile and req.profile != "auto" and req.profile not in gateway.profile_ids:
        raise ParamError(f"模型 profile 不存在或未配置: {req.profile}")
    if req.mode == "direct" and not gateway.configured:
        raise UpstreamError("直连模式需要配置模型网关（server/.env）")
    folder = None
    if req.folder:
        try:
            folder = normalize_folder(req.folder, default=None)
        except FolderPathError as e:
            raise ParamError(str(e)) from e
    folders: list[str] = []
    for item in req.folders or []:
        try:
            path = normalize_folder(item, default=None)
        except FolderPathError as e:
            raise ParamError(str(e)) from e
        if path and path not in folders:
            folders.append(path)
    if folder and folder not in folders:
        folders.insert(0, folder)
    folder = None

    async def event_gen():
        async for ev in pipeline.chat_stream(
            req.message,
            req.mode,
            req.top_k,
            settings,
            session_id=req.session_id,
            profile=None if req.profile in (None, "auto") else req.profile,
            folder=folder,
            folders=folders or None,
            response_format=req.response_format,
            attachments=attachments or None,
        ):
            if ev["event"] == "done":
                ev["data"]["request_id"] = rid
            data = json.dumps(ev["data"], ensure_ascii=False)
            yield f"event: {ev['event']}\ndata: {data}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
