"""嵌入后端：Ollama（REST，中英效果好）与 Chroma 内置 MiniLM（零依赖回退）双通道。

auto 策略每次解析时探测一次 Ollama（单次 HTTP 开销毫秒级），
探测失败立即回退默认后端，保证离线/演示场景不中断。
"""

import json
import time
import urllib.error
import urllib.request

import httpx


class BaseEmbedder:
    backend_id: str

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class ChromaDefaultEmbedder(BaseEmbedder):
    """零配置回退通道。

    优先 fastembed 本地常驻 ONNX（bge-small-zh，中文友好、无网络抖动）；
    未安装 fastembed 时退回 chromadb 内置 MiniLM（其每次调用会做模型下载校验，
    弱网下查询嵌入可达数秒，仅作兜底）。
    """

    def __init__(self):
        try:
            # 弱网环境默认走 HF 镜像下载，模型缓存后离线可用（不覆盖显式配置）
            import os

            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            from fastembed import TextEmbedding

            self._model = TextEmbedding("BAAI/bge-small-zh-v1.5")
            self.backend_id = "bge-small-zh"
        except ImportError:
            from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

            self._model = None
            self._fn = DefaultEmbeddingFunction()
            self.backend_id = "minilm"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self._model is not None:
            return [[float(x) for x in vec] for vec in self._model.embed(texts)]
        # chroma 1.x 要求纯 Python float，numpy 标量会被拒绝
        return [[float(x) for x in vec] for vec in self._fn(texts)]


class OllamaEmbedder(BaseEmbedder):
    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.backend_id = f"ollama-{model.replace(':', '-')}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = httpx.post(
            f"{self.base_url}/api/embed",
            json={"model": self.model, "input": texts},
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        if "embeddings" not in data:
            raise RuntimeError(f"Ollama 嵌入响应缺少 embeddings 字段: {list(data)}")
        return data["embeddings"]


_probe_cache: dict[str, tuple[float, bool]] = {}
_PROBE_TTL = 30.0


def _ollama_available(base_url: str, model: str) -> bool:
    """探测结果带 TTL 缓存：auto 模式每次检索都会调用本函数，
    而防火墙对未监听端口的静默丢弃会耗满超时，无缓存时每查询白付数秒。"""
    key = f"{base_url}|{model}"
    now = time.monotonic()
    hit = _probe_cache.get(key)
    if hit and now - hit[0] < _PROBE_TTL:
        return hit[1]
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/tags", timeout=2) as resp:
            names = [m.get("name", "") for m in json.loads(resp.read()).get("models", [])]
        ok = any(n.split(":")[0] == model.split(":")[0] for n in names)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        ok = False
    _probe_cache[key] = (now, ok)
    return ok


_embedder_cache: dict[tuple, BaseEmbedder] = {}


def get_embedder(settings) -> BaseEmbedder:
    backend = settings.embed_backend
    if backend == "auto":
        backend = "ollama" if _ollama_available(settings.ollama_base_url, settings.ollama_embed_model) else "default"
    if backend == "ollama":
        key = ("ollama", settings.ollama_base_url, settings.ollama_embed_model)
        if key not in _embedder_cache:
            _embedder_cache[key] = OllamaEmbedder(settings.ollama_base_url, settings.ollama_embed_model)
    else:
        key = ("default",)
        if key not in _embedder_cache:
            _embedder_cache[key] = ChromaDefaultEmbedder()
    return _embedder_cache[key]
