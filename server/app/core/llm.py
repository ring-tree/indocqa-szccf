"""模型网关：OpenAI 兼容接口，主备供应商自动切换（SSE 流式透传）。

生成参数（temperature / max_tokens）在网关层按用途固化，公开方法不接受数值覆盖；
仅对不支持可选参数的模型做显式降级重试，实际发送参数通过 last_request 回传。
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
        self.settings = settings
        self.providers: list[dict] = []
        self.disable_thinking = settings.llm_disable_thinking
        self._seen: set[tuple[str, str, str]] = set()

        def add_provider(pid: str, base: str, key: str, model: str) -> None:
            if not (base and _usable_key(key) and model):
                return
            signature = (base.rstrip("/"), key, model)
            if signature in self._seen:
                return
            self._seen.add(signature)
            self.providers.append(
                {
                    "id": pid,
                    "base": base.rstrip("/"),
                    "key": key,
                    "model": model,
                    # 第一个可用供应商作为主供应商，后续依次作为故障切换候选
                    "primary": not self.providers,
                }
            )

        add_provider("primary", settings.llm_base_url, settings.llm_api_key, settings.llm_model)
        add_provider(
            "backup",
            settings.llm_backup_base_url,
            settings.llm_backup_api_key,
            settings.llm_backup_model,
        )
        for index, item in enumerate(getattr(settings, "llm_profiles", []) or [], start=1):
            add_provider(
                str(item.get("id") or f"profile{index}"),
                str(item.get("base_url") or ""),
                str(item.get("api_key") or ""),
                str(item.get("model") or ""),
            )
        self.profile = profile if profile in {p["id"] for p in self.providers} else None
        self.last_profile: str | None = None
        self.last_model: str | None = None
        # 最近一次实际发送的生成参数（含参数兼容降级结果）
        self.last_request: dict | None = None

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

    def _preset(self, purpose: str) -> tuple[float, int]:
        """按固定用途读取配置，禁止调用方覆盖。"""
        if purpose == "json":
            return (
                self.settings.generation_json_temperature,
                self.settings.generation_json_max_tokens,
            )
        if purpose == "eval":
            return (
                self.settings.generation_eval_temperature,
                self.settings.generation_eval_max_tokens,
            )
        return self.settings.generation_temperature, self.settings.generation_max_tokens

    def _build_body(
        self,
        provider: dict,
        messages: list[dict],
        json_mode: bool = False,
        purpose: str = "default",
        include_temperature: bool = True,
        include_response_format: bool = True,
    ) -> dict:
        temperature, max_tokens = self._preset("json" if json_mode else purpose)
        body: dict = {
            "model": provider["model"],
            "messages": messages,
            "stream": True,
            "max_tokens": max_tokens,
        }
        if include_temperature:
            body["temperature"] = temperature
        if provider["primary"] and self.disable_thinking:
            # 思考型模型关闭思考，降低首 token 延迟；备用供应商不注入该参数
            body["enable_thinking"] = False
        if json_mode and include_response_format:
            body["response_format"] = {"type": "json_object"}
        return body

    def _record_request(
        self,
        body: dict,
        json_mode: bool,
        purpose: str,
        temperature_configured: float,
        max_tokens_configured: int,
    ) -> None:
        self.last_request = {
            "temperature": body.get("temperature"),
            "maxTokens": body.get("max_tokens"),
            "jsonMode": bool(body.get("response_format")),
            "responseFormat": bool(body.get("response_format")),
            "purpose": purpose,
            "temperatureConfigured": temperature_configured,
            "maxTokensConfigured": max_tokens_configured,
            "temperatureSupported": "temperature" in body,
            "responseFormatSupported": "response_format" in body,
        }

    async def _stream_once(self, provider: dict, body: dict) -> AsyncIterator[str]:
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
            idle_timeout = float(getattr(self.settings, "llm_stream_idle_timeout", 30.0))
            while True:
                try:
                    if first_line:
                        # 首字节超时：供应商排队/冷启动时尽快切换，不拖满 read timeout
                        line = await asyncio.wait_for(ait.__anext__(), timeout=3.0)
                        first_line = False
                    else:
                        # 流式空闲超时：连接建立后长时间无数据时中止，避免请求无限挂起
                        line = await asyncio.wait_for(ait.__anext__(), timeout=idle_timeout)
                except StopAsyncIteration:
                    break
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    return
                choices = json.loads(payload).get("choices") or []
                if not choices:
                    continue  # usage 统计帧等无增量内容
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield content

    async def stream(
        self,
        messages: list[dict],
        json_mode: bool = False,
        purpose: str = "default",
    ) -> AsyncIterator[str]:
        errors: list[str] = []
        configured_temperature, configured_max_tokens = self._preset(
            "json" if json_mode else purpose
        )
        for provider in self._ordered():
            produced = False
            # 参数兼容降级：只减少可选参数，不改变配置值；max_tokens 始终保留。
            attempts = [(json_mode, True, True)]
            if json_mode:
                attempts.extend(
                    [
                        (json_mode, True, False),
                        (json_mode, False, False),
                        (False, True, False),
                    ]
                )
            else:
                attempts.append((False, False, True))
            for native_json, include_temperature, include_response_format in attempts:
                preset_purpose = "json" if json_mode else purpose
                body = self._build_body(
                    provider,
                    messages,
                    json_mode=native_json,
                    purpose=preset_purpose,
                    include_temperature=include_temperature,
                    include_response_format=include_response_format,
                )
                try:
                    self._record_request(
                        body,
                        native_json,
                        preset_purpose,
                        configured_temperature,
                        configured_max_tokens,
                    )
                    self.last_request["jsonExpected"] = json_mode
                    async for delta in self._stream_once(provider, body):
                        produced = True
                        yield delta
                    self._mark_used(provider)
                    return
                except UpstreamError as e:
                    if produced:
                        raise
                    errors.append(f"{provider['base']}: {e.message}")
                    continue  # 400 类参数不兼容：去掉可选参数后重试
                except (TimeoutError, httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError) as e:
                    if produced:
                        raise UpstreamError("模型流式响应超时或中断", detail=str(e)) from e
                    errors.append(f"{provider['base']}: {type(e).__name__} {e}")
                    break
        raise UpstreamError("模型网关不可用（主备均失败）", detail="; ".join(errors))

    def _mark_used(self, provider: dict) -> None:
        self.last_profile = provider["id"]
        self.last_model = provider["model"]

    async def complete(self, messages: list[dict], purpose: str = "default") -> str:
        """非流式补全（置信度自评等轻量调用），聚合流式实现。"""
        chunks = [delta async for delta in self.stream(messages, purpose=purpose)]
        return "".join(chunks)
