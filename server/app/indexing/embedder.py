"""嵌入层：三通道可选，策略由 INDOCQA_EMBED_BACKEND 控制（auto/local/ollama/minilm）。

- local：fastembed 进程内常驻 ONNX，模型由 INDOCQA_EMBED_LOCAL_MODEL 指定，
  权重缓存在本地后离线可用（Docker 自包含部署的默认通道）
- ollama：本机 Ollama /api/embed，质量好但引入外部服务依赖与 HTTP 延迟
- minilm：chromadb 内置兜底（其每次调用做模型下载校验，弱网下秒级延迟，仅保底）

auto：Ollama 在运行且模型存在则用 ollama，否则 local。
向量库集合名携带通道与模型标识，切换后需删除 data/chroma 重建。
"""

import json
import time
import urllib.error
import urllib.request

import httpx

_probe_cache: dict[str, tuple[float, bool]] = {}
_PROBE_TTL = 30.0
_BATCH = 32


class BaseEmbedder:
    backend_id: str

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def query_embed(self, texts: list[str]) -> list[list[float]]:
        """查询侧嵌入；e5 等模型需要与文档侧不同的前缀，其余模型与 embed 相同。"""
        return self.embed(texts)


class LocalEmbedder(BaseEmbedder):
    """fastembed 进程内推理：模型一次加载常驻，查询毫秒级。

    批量控制在 embed() 调用侧（fastembed 0.9 构造函数不接受 batch_size，
    其默认 256 条批量在 CPU 上对大模型会触发数 GB 注意力缓冲分配失败）。
    """

    def __init__(self, model_name: str, batch_size: int = 32, cache_dir: str | None = None):
        # 模型权重固定存放在项目内 models/ 目录（随项目打包分发，不依赖项目外缓存）；
        # 首次使用时经 HF 镜像下载，此后完全离线
        import os
        import pathlib

        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        from fastembed import TextEmbedding

        if cache_dir:
            pathlib.Path(cache_dir).mkdir(parents=True, exist_ok=True)
        self._model = TextEmbedding(model_name, cache_dir=cache_dir)
        self._batch = batch_size
        self.backend_id = "fe-" + model_name.split("/")[-1].replace(":", "-")

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(x) for x in vec] for vec in self._model.embed(texts, batch_size=self._batch)]

    def query_embed(self, texts: list[str]) -> list[list[float]]:
        if hasattr(self._model, "query_embed"):
            return [[float(x) for x in vec] for vec in self._model.query_embed(texts)]
        return self.embed(texts)


class OllamaEmbedder(BaseEmbedder):
    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.backend_id = f"ollama-{model.replace(':', '-')}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _BATCH):
            batch = texts[start : start + _BATCH]
            resp = httpx.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": batch},
                timeout=300,
            )
            resp.raise_for_status()
            data = resp.json()
            if "embeddings" not in data:
                raise RuntimeError(f"Ollama 嵌入响应缺少 embeddings 字段: {list(data)}")
            vectors.extend(data["embeddings"])
        return vectors


class MiniLMEmbedder(BaseEmbedder):
    """chromadb 内置兜底通道，仅在 fastembed 不可用时使用。"""

    backend_id = "minilm"

    def __init__(self):
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

        self._fn = DefaultEmbeddingFunction()

    def embed(self, texts: list[str]) -> list[list[float]]:
        # chroma 1.x 要求纯 Python float，numpy 标量会被拒绝
        return [[float(x) for x in vec] for vec in self._fn(texts)]


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
        backend = (
            "ollama"
            if _ollama_available(settings.ollama_base_url, settings.ollama_embed_model)
            else "local"
        )
    if backend == "ollama":
        key = ("ollama", settings.ollama_base_url, settings.ollama_embed_model)
        if key not in _embedder_cache:
            _embedder_cache[key] = OllamaEmbedder(
                settings.ollama_base_url, settings.ollama_embed_model
            )
    elif backend == "minilm":
        key = ("minilm",)
        if key not in _embedder_cache:
            _embedder_cache[key] = MiniLMEmbedder()
    else:
        key = ("local", settings.embed_local_model, settings.embed_local_batch, settings.embed_cache_dir)
        if key not in _embedder_cache:
            _embedder_cache[key] = LocalEmbedder(
                settings.embed_local_model, settings.embed_local_batch, settings.embed_cache_dir
            )
    return _embedder_cache[key]
