"""模型网关：OpenAI 兼容接口，主备供应商自动切换（SSE 流式透传）。

中途已产出内容后不再切换供应商，避免重复输出；仅在尚未产出时切换。
"""

import json
from collections.abc import AsyncIterator

import httpx

from app.core.config import Settings
from app.core.exceptions import UpstreamError

_SWITCHABLE_STATUS = {429, 500, 502, 503, 504}


class LLMGateway:
    def __init__(self, settings: Settings):
        self.providers: list[dict] = []
        for base, key, model in (
            (settings.llm_base_url, settings.llm_api_key, settings.llm_model),
            (settings.llm_backup_base_url, settings.llm_backup_api_key, settings.llm_backup_model),
        ):
            if base and key and model:
                self.providers.append({"base": base.rstrip("/"), "key": key, "model": model})

    @property
    def configured(self) -> bool:
        return bool(self.providers)

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        errors: list[str] = []
        for provider in self.providers:
            produced = False
            try:
                async with httpx.AsyncClient(timeout=90) as client:
                    async with client.stream(
                        "POST",
                        f"{provider['base']}/chat/completions",
                        headers={"Authorization": f"Bearer {provider['key']}"},
                        json={"model": provider["model"], "messages": messages, "stream": True},
                    ) as resp:
                        if resp.status_code in _SWITCHABLE_STATUS:
                            raise httpx.HTTPStatusError(
                                f"HTTP {resp.status_code}", request=resp.request, response=resp
                            )
                        if resp.status_code >= 400:
                            raise UpstreamError(
                                f"上游模型返回 HTTP {resp.status_code}", detail=provider["base"]
                            )
                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if payload == "[DONE]":
                                return
                            delta = (
                                json.loads(payload)["choices"][0]["delta"].get("content")
                            )
                            if delta:
                                produced = True
                                yield delta
                        return
            except UpstreamError:
                raise
            except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError) as e:
                if produced:
                    raise UpstreamError("模型流式中断", detail=str(e)) from e
                errors.append(f"{provider['base']}: {type(e).__name__} {e}")
        raise UpstreamError("模型网关不可用（主备均失败）", detail="; ".join(errors))

    async def complete(self, messages: list[dict]) -> str:
        """非流式补全（置信度自评等轻量调用），聚合流式实现。"""
        chunks = [delta async for delta in self.stream(messages)]
        return "".join(chunks)
