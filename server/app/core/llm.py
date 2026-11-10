"""模型网关：OpenAI 兼容接口，主备供应商自动切换（SSE 流式透传）。

中途已产出内容后不再切换供应商，避免重复输出；仅在尚未产出时切换。
支持按 profile（primary / backup）指定首选供应商，失败时仍回退到其余已配置供应商。
"""

import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from app.core.config import Settings
from app.core.exceptions import UpstreamError

_SWITCHABLE_STATUS = {429, 500, 502, 503, 504}
_PLACEHOLDER_PREFIXES = ("sk-your", "your-", "replace", "changeme", "change-me", "todo", "xxx")


def _usable_key(key: str) -> bool:
    """过滤 .env.example 复制后未替换的占位 Key，避免把备用模型当成可用。"""
    value = (key or "").strip().lower()
    return len(value) >= 12 and not value.startswith(_PLACEHOLDER_PREFIXES)


class LLMGateway:
    def __init__(self, settings: Settings, profile: str | None = None):
        self.providers: list[dict] = []
        self.disable_thinking = settings.llm_disable_thinking
        for pid, base, key, model, primary in (
            (
                "primary",
                settings.llm_base_url,
                settings.llm_api_key,
                settings.llm_model,
                True,
            ),
            (
                "backup",
                settings.llm_backup_base_url,
                settings.llm_backup_api_key,
                settings.llm_backup_model,
                False,
            ),
        ):
            if base and _usable_key(key) and model:
                self.providers.append(
                    {
                        "id": pid,
                        "base": base.rstrip("/"),
                        "key": key,
                        "model": model,
                        "primary": primary,
                    }
                )
        self.profile = profile if profile in {p["id"] for p in self.providers} else None
        # 最近一次实际完成的供应商，供 done 事件回传
        self.last_profile: str | None = None
        self.last_model: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.providers)

    @property
    def profile_ids(self) -> set[str]:
        return {p["id"] for p in self.providers}

    def profiles(self) -> list[dict]:
        """可对外暴露的模型清单，不含 key/base。"""
        return [{"id": p["id"], "model": p["model"]} for p in self.providers]

    def _ordered(self) -> list[dict]:
        if not self.profile:
            return self.providers
        preferred = [p for p in self.providers if p["id"] == self.profile]
        return preferred + [p for p in self.providers if p["id"] != self.profile]

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        errors: list[str] = []
        for provider in self._ordered():
            produced = False
            body: dict = {"model": provider["model"], "messages": messages, "stream": True}
            if provider["primary"] and self.disable_thinking:
                # 思考型模型关闭思考，降低首 token 延迟；备用供应商不注入该参数
                body["enable_thinking"] = False
            try:
                async with (
                    httpx.AsyncClient(timeout=90) as client,
                    client.stream(
                        "POST",
                        f"{provider['base']}/chat/completions",
                        headers={"Authorization": f"Bearer {provider['key']}"},
                        json=body,
                    ) as resp,
                ):
                    if resp.status_code in _SWITCHABLE_STATUS:
                        raise httpx.HTTPStatusError(
                            f"HTTP {resp.status_code}", request=resp.request, response=resp
                        )
                    if resp.status_code >= 400:
                        raise UpstreamError(
                            f"上游模型返回 HTTP {resp.status_code}", detail=provider["base"]
                        )
                    ait = resp.aiter_lines().__aiter__()
                    first_line = True
                    while True:
                        try:
                            if first_line:
                                # 首字节超时：供应商排队/冷启动时尽快切换，不拖满 read timeout
                                line = await asyncio.wait_for(ait.__anext__(), timeout=3.0)
                                first_line = False
                            else:
                                line = await ait.__anext__()
                        except StopAsyncIteration:
                            break
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            self._mark_used(provider)
                            return
                        choices = json.loads(payload).get("choices") or []
                        if not choices:
                            continue  # usage 统计帧等无增量内容
                        delta = choices[0].get("delta") or {}
                        content = delta.get("content")
                        if content:
                            produced = True
                            yield content
                    self._mark_used(provider)
                    return
            except UpstreamError:
                raise
            except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError) as e:
                if produced:
                    raise UpstreamError("模型流式中断", detail=str(e)) from e
                errors.append(f"{provider['base']}: {type(e).__name__} {e}")
        raise UpstreamError("模型网关不可用（主备均失败）", detail="; ".join(errors))

    def _mark_used(self, provider: dict) -> None:
        self.last_profile = provider["id"]
        self.last_model = provider["model"]

    async def complete(self, messages: list[dict]) -> str:
        """非流式补全（置信度自评等轻量调用），聚合流式实现。"""
        chunks = [delta async for delta in self.stream(messages)]
        return "".join(chunks)
