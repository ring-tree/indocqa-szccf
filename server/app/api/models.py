"""模型清单接口：只暴露已配置的输出层模型，不含密钥与上游地址。"""

from fastapi import APIRouter

from app.core.config import get_settings
from app.core.llm import LLMGateway

router = APIRouter(prefix="/api")


@router.get("/models")
def models() -> dict:
    return {"data": LLMGateway(get_settings()).profiles()}
