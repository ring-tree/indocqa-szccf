"""健康检查：服务版本、知识库规模、模型网关配置状态。"""

import pathlib

from fastapi import APIRouter

from app.api.schemas import EmbeddingStatus, HealthResponse, KBStats, LLMStatus
from app.core.config import get_settings
from app.indexing.embedder import backend_id_for
from app.indexing.manifest import Manifest

router = APIRouter(prefix="/api")


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    docs, chunks = Manifest(pathlib.Path(settings.chroma_dir) / "manifest.json").stats()
    state = "ready" if chunks > 0 else ("empty" if docs > 0 else "not_indexed")
    backend_id = backend_id_for(settings)
    if backend_id.startswith("ollama-"):
        model = settings.ollama_embed_model
    elif backend_id.startswith("fe-"):
        model = settings.embed_local_model
    else:
        model = "chroma-default"
    return HealthResponse(
        status="ok",
        version=settings.version,
        kb=KBStats(documents=docs, chunks=chunks, state=state),
        llm=LLMStatus(configured=bool(settings.llm_base_url and settings.llm_api_key)),
        embedding=EmbeddingStatus(
            mode=settings.embed_backend,
            backend=backend_id,
            model=model,
        ),
    )
