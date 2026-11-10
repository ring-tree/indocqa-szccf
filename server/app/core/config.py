"""应用配置：全部来自环境变量（前缀 INDOCQA_），见 server/.env.example。"""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVER_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INDOCQA_", env_file=".env", extra="ignore")

    @field_validator(
        "chroma_dir",
        "upload_dir",
        "corpus_dir",
        "terms_file",
        "embed_cache_dir",
        "session_db",
        mode="after",
    )
    @classmethod
    def _anchor_to_server(cls, value: str) -> str:
        """相对路径一律锚定到 server/ 目录，与运行时 cwd 无关。"""
        p = Path(value)
        return str(p if p.is_absolute() else (SERVER_ROOT / p).resolve())

    app_name: str = "IndocQA"
    version: str = "0.1.0"

    # 模型网关：主供应商 + 备用供应商，主供应商限流或故障时切换
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_backup_base_url: str = ""
    llm_backup_api_key: str = ""
    llm_backup_model: str = ""
    # 思考型模型（qwen3 系）关闭思考阶段以降低首 token 延迟；仅注入主供应商请求
    llm_disable_thinking: bool = True

    embed_model: str = "BAAI/bge-m3"
    # auto=优先 Ollama（运行中且模型存在时），否则 local；可显式 local / ollama / minilm
    embed_backend: str = "auto"
    embed_local_model: str = "BAAI/bge-small-zh-v1.5"
    embed_local_batch: int = 32
    # 本地模型权重随项目分发：仓库根 models/ 目录（打包时连同复制，程序只引用项目内路径）
    embed_cache_dir: str = "../models"
    ollama_base_url: str = "http://localhost:11434"
    ollama_embed_model: str = "bge-m3"

    chroma_dir: str = "data/chroma"
    corpus_dir: str = "../data/corpus"
    upload_dir: str = "data/uploads"
    # 会话历史：SQLite 单文件，随 data/ 忽略；保留轮数与过期天数可按部署调整
    session_db: str = "data/db/sessions.db"
    session_history_turns: int = 6
    session_ttl_days: int = 7

    # fixed=固定参数基线（评测对照组）；adaptive=意图路由+参数自适应
    retrieval_mode: str = "adaptive"
    top_k_default: int = 6
    sim_threshold: float = 0.20
    conf_high: float = 0.42
    # 弱命中闸门：最高相似度低于该值时按“库外问题”处理（bge-m3 无关文本约 0.4–0.5）
    boundary_min_sim: float = 0.50
    terms_file: str = "../data/terms/terms.json"
    use_rerank: bool = False
    # 消融实验开关：关闭后 adaptive 模式不再做查询侧术语扩展（索引侧打标保留，属部分消融）
    term_expansion_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
