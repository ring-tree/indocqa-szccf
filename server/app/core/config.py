"""应用配置：全部来自环境变量（前缀 INDOCQA_），见 server/.env.example。"""

import os
import re
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVER_ROOT = Path(__file__).resolve().parents[2]
_PROFILE_ENV = re.compile(r"^INDOCQA_LLM_PROFILE_(\d+)_(BASE_URL|API_KEY|MODEL|NAME)$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INDOCQA_", env_file=".env", extra="ignore")

    @field_validator(
        "chroma_dir",
        "upload_dir",
        "attachment_dir",
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
    # 模型流式空闲超时：连接建立后长时间没有新数据则中止，避免请求无限挂起
    llm_stream_idle_timeout: float = Field(default=30.0, ge=3.0, le=120.0)
    # 检索阶段整体超时（含嵌入服务），超时后返回可重试错误而不是继续阻塞
    retrieval_timeout: float = Field(default=45.0, ge=5.0, le=180.0)
    # 任意多套额外模型：INDOCQA_LLM_PROFILE_<N>_{BASE_URL,API_KEY,MODEL,NAME}
    # 由 model_validator 从 .env / 环境变量聚合，不需要为每套声明字段
    llm_profiles: list[dict] = Field(default_factory=list, repr=False, exclude=True)
    # 思考型模型（qwen3 系）关闭思考阶段以降低首 token 延迟；仅注入主供应商请求
    llm_disable_thinking: bool = True
    # 生成参数固化：温度、最大生成长度（结构化模式单独设定，保证 JSON 完整）
    generation_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    generation_max_tokens: int = Field(default=1024, ge=64, le=8192)
    generation_json_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    generation_json_max_tokens: int = Field(default=1536, ge=64, le=8192)
    # 内部自评等轻量调用使用独立固定预设，调用方不能覆盖
    generation_eval_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    generation_eval_max_tokens: int = Field(default=32, ge=1, le=256)

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
    # 会话附件：与知识库上传目录分离，按 session 隔离存储
    attachment_dir: str = "data/attachments"
    attachment_max_bytes: int = Field(default=100 * 1024 * 1024, ge=1024 * 1024)
    attachment_max_files: int = Field(default=10, ge=1, le=50)
    attachment_chunk_bytes: int = Field(default=4 * 1024 * 1024, ge=256 * 1024)
    attachment_staging_ttl_hours: int = Field(default=24, ge=1, le=720)
    attachment_text_chars_per_file: int = Field(default=12000, ge=1000, le=200000)
    attachment_text_chars_total: int = Field(default=30000, ge=1000, le=500000)
    attachment_recent_limit: int = Field(default=3, ge=0, le=10)
    attachment_max_hits: int = Field(default=8, ge=1, le=30)
    attachment_hits_per_file: int = Field(default=2, ge=1, le=6)
    # 视觉理解默认关闭；开启后要求当前模型支持 OpenAI 兼容 image_url content parts
    attachment_vision: bool = False
    attachment_vision_max_images: int = Field(default=3, ge=1, le=10)
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
    # 本地 fastembed 小模型的相似度分布整体偏低（相关块约 0.45–0.55），单独设阈值
    boundary_min_sim_local: float = 0.40
    terms_file: str = "../data/terms/terms.json"
    use_rerank: bool = False
    # 消融实验开关：关闭后 adaptive 模式不再做查询侧术语扩展（索引侧打标保留，属部分消融）
    term_expansion_enabled: bool = True

    # 上下文组装：on=生效；shadow=只统计不生效；off=回退旧拼接
    context_enabled: bool = True
    context_mode: str = "on"
    context_window_tokens: int = 32768
    context_output_reserve: int = 1500
    context_safety_ratio: float = 0.90
    context_estimate_margin: float = 1.08
    context_quota_evidence: float = 0.60
    context_quota_history: float = 0.18
    context_quota_task: float = 0.14
    context_quota_persona: float = 0.08
    context_dedup_semantic: float = 0.92
    context_dedup_semantic_enabled: bool = True
    context_dedup_hamming: int = 3
    context_dedup_jaccard: float = 0.82
    context_noise_score_ratio: float = 0.35
    context_weights: str = ""
    context_template_id: str = "kb-answer"
    context_template_version: str = "1"
    context_trace_persist: bool = True
    context_persona_enabled: bool = True

    @model_validator(mode="after")
    def _collect_llm_profiles(self):
        """聚合 INDOCQA_LLM_PROFILE_<N>_*，支持任意多套模型配置。"""
        values: dict[str, str] = {}
        env_file = SERVER_ROOT / ".env"
        if env_file.exists():
            try:
                values.update(
                    {
                        key: value
                        for key, value in dotenv_values(env_file).items()
                        if value is not None
                    }
                )
            except (OSError, ValueError):
                pass
        values.update(
            {key: value for key, value in os.environ.items() if key.startswith("INDOCQA_")}
        )
        grouped: dict[int, dict[str, str]] = {}
        for key, value in values.items():
            match = _PROFILE_ENV.match(key)
            if not match:
                continue
            index, field_name = int(match.group(1)), match.group(2).lower()
            grouped.setdefault(index, {})[field_name] = str(value).strip()
        profiles = []
        for index in sorted(grouped):
            item = grouped[index]
            if item.get("base_url") and item.get("api_key") and item.get("model"):
                profiles.append(
                    {
                        "id": f"profile{index}",
                        "base_url": item["base_url"],
                        "api_key": item["api_key"],
                        "model": item["model"],
                        "name": item.get("name") or item["model"],
                    }
                )
        self.llm_profiles = profiles
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
