"""API 数据模型（与 docs/openapi.yaml 保持一致，契约测试校验）。"""

from pydantic import BaseModel, Field


class KBStats(BaseModel):
    documents: int | None = None
    chunks: int | None = None
    state: str = "not_indexed"


class LLMStatus(BaseModel):
    configured: bool = False
    reachable: bool | None = None


class EmbeddingStatus(BaseModel):
    mode: str = "auto"
    backend: str = ""
    model: str = ""


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    kb: KBStats
    llm: LLMStatus
    embedding: EmbeddingStatus = EmbeddingStatus()


class ChatRequest(BaseModel):
    session_id: str | None = Field(default=None, alias="sessionId", max_length=64)
    message: str = Field(min_length=1, max_length=2000)
    mode: str = Field(pattern="^(direct|kb)$")
    top_k: int | None = Field(default=None, ge=1, le=20, alias="topK")
    # auto / primary / backup；不传等同 auto
    profile: str | None = Field(default=None, max_length=32)
    # 知识库目录检索范围（空=全库）；folders 支持多选目录
    folder: str | None = Field(default=None, max_length=200)
    folders: list[str] | None = Field(default=None, max_length=20)

    model_config = {"populate_by_name": True}
