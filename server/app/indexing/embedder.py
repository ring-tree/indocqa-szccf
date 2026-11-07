"""嵌入后端：Ollama（REST，中英效果好）与 Chroma 内置 MiniLM（零依赖回退）双通道。

auto 策略每次解析时探测一次 Ollama（单次 HTTP 开销毫秒级），
探测失败立即回退默认后端，保证离线/演示场景不中断。
"""

import json
import urllib.error
import urllib.request

import httpx


class BaseEmbedder:
    backend_id: str

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class ChromaDefaultEmbedder(BaseEmbedder):
    backend_id = "minilm"

    def __init__(self):
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

        self._fn = DefaultEmbeddingFunction()

    def embed(self, texts: list[str]) -> list[list[float]]:
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


def _ollama_available(base_url: str, model: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/tags", timeout=2) as resp:
            names = [m.get("name", "") for m in json.loads(resp.read()).get("models", [])]
        return any(n.split(":")[0] == model.split(":")[0] for n in names)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


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
