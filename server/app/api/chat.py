"""问答接口：SSE 流式（token / sources / done / error 事件见 openapi.yaml）。"""

import json
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api.schemas import ChatRequest
from app.core.config import get_settings
from app.core.exceptions import KBEmptyError, UpstreamError
from app.core.llm import LLMGateway
from app.graph import pipeline

router = APIRouter(prefix="/api")


@router.post("/chat")
async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    settings = get_settings()
    rid = request.headers.get("X-Request-Id") or uuid4().hex[:12]

    if req.mode == "kb" and pipeline.kb_is_empty(settings):
        raise KBEmptyError("知识库为空，请先入库文档（POST /api/kb/ingest）")
    if req.mode == "direct" and not LLMGateway(settings).configured:
        raise UpstreamError("直连模式需要配置模型网关（server/.env）")

    async def event_gen():
        async for ev in pipeline.chat_stream(req.message, req.mode, req.top_k, settings):
            if ev["event"] == "done":
                ev["data"]["request_id"] = rid
            data = json.dumps(ev["data"], ensure_ascii=False)
            yield f"event: {ev['event']}\ndata: {data}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
