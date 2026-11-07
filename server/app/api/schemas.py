"""API 数据模型（与 docs/openapi.yaml 保持一致，契约测试校验）。"""

from pydantic import BaseModel, Field


class KBStats(BaseModel):
    documents: int | None = None
    chunks: int | None = None
    state: str = "not_indexed"


class LLMStatus(BaseModel):
    configured: bool = False
    reachable: bool | None = None


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    kb: KBStats
    llm: LLMStatus


class ChatRequest(BaseModel):
    session_id: str | None = Field(default=None, alias="sessionId")
    message: str = Field(min_length=1, max_length=2000)
    mode: str = Field(pattern="^(direct|kb)$")
    top_k: int | None = Field(default=None, ge=1, le=20, alias="topK")

    model_config = {"populate_by_name": True}
