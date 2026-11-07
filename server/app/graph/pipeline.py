"""问答编排：LangGraph 检索状态图 + 流式生成 + 事后置信度与引用校验。

SSE 事件顺序：trace* → sources → token* → done（错误时以 error 事件收尾）。
"""

import asyncio
import re
from collections.abc import AsyncIterator

from app.core.config import get_settings
from app.core.exceptions import UpstreamError
from app.core.llm import LLMGateway
from app.generation.answerer import REFUSAL_TEMPLATE, stream_answer
from app.graph.graph import run_retrieval
from app.indexing import store
from app.retrieval.searcher import Hit

_LEVEL = {"low": 0, "mid": 1, "high": 2}
_CITED = re.compile(r"\[(\d+)\]")
_CHITCHAT_SYSTEM = (
    "你是企业知识库助手的门面。对寒暄给出简短友好的回应，一两句话即可，不涉及任何技术内容。"
)


def _source_item(hit: Hit, idx: int) -> dict:
    return {
        "index": idx,
        "docId": hit.doc_id,
        "docName": hit.doc_name,
        "section": hit.section,
        "page": hit.page,
        "locator": hit.table_no,
        "score": round(hit.sim, 4) if hit.sim is not None else None,
    }


async def chat_stream(
    message: str, mode: str, top_k: int | None = None, settings=None
) -> AsyncIterator[dict]:
    settings = settings or get_settings()
    gateway = LLMGateway(settings)

    if mode == "direct":
        if not gateway.configured:
            raise UpstreamError("直连模式需要配置模型网关（server/.env）")
        try:
            async for delta in gateway.stream([{"role": "user", "content": message}]):
                yield {"event": "token", "data": {"delta": delta}}
        except UpstreamError as e:
            yield {"event": "error", "data": {"code": e.code, "message": e.message}}
            return
        yield {"event": "done", "data": {"confidence": None, "confidence_reason": ""}}
        return

    retrieval = await run_retrieval(message, settings.retrieval_mode, settings)
    for step in retrieval["trace"]:
        yield {"event": "trace", "data": step}

    plan = retrieval["plan"]
    hits = retrieval["hits"]

    if plan.get("skip_retrieval"):
        async for ev in _chitchat_reply(message, gateway):
            yield ev
        return

    yield {
        "event": "sources",
        "data": {"items": [_source_item(h, i) for i, h in enumerate(hits, 1)]},
    }

    if not hits:
        yield {
            "event": "token",
            "data": {"delta": REFUSAL_TEMPLATE.format(sections="全部已入库文档")},
        }
        yield {"event": "done", "data": _done("low", "检索无有效命中，答案为拒答说明")}
        return

    answer_parts: list[str] = []
    try:
        async for delta in stream_answer(message, hits, gateway):
            answer_parts.append(delta)
            yield {"event": "token", "data": {"delta": delta}}
    except UpstreamError as e:
        yield {"event": "error", "data": {"code": e.code, "message": e.message}}
        return

    answer = "".join(answer_parts)
    _cov, has_valid_citation = _citation_coverage(answer, hits)
    if hits and not has_valid_citation:
        # 引用补正：回答缺少有效引用时带强指令重生成一次（可从 trace 观察）
        yield {
            "event": "trace",
            "data": {"stage": "citation_retry", "result": "回答缺少有效引用，重生成补正"},
        }
        answer_parts = []
        retry_question = (
            f"{message}\n（必须做到：每个事实句末标注引用编号，如 [1] [2]，编号对应资料标号）"
        )
        try:
            yield {"event": "token", "data": {"delta": "\n——引用补正——\n"}}
            async for delta in stream_answer(retry_question, hits, gateway):
                answer_parts.append(delta)
                yield {"event": "token", "data": {"delta": delta}}
        except UpstreamError:
            pass  # 补正失败保留原答案
        if answer_parts:
            answer = "".join(answer_parts)

    level, reason = await _confidence(message, answer, hits, gateway, settings)
    yield {"event": "done", "data": _done(level, reason)}


async def _chitchat_reply(message: str, gateway: LLMGateway) -> AsyncIterator[dict]:
    if gateway.configured:
        try:
            async for delta in gateway.stream(
                [
                    {"role": "system", "content": _CHITCHAT_SYSTEM},
                    {"role": "user", "content": message},
                ]
            ):
                yield {"event": "token", "data": {"delta": delta}}
        except UpstreamError:
            yield {"event": "token", "data": {"delta": "你好，有问题随时问我。"}}
    else:
        yield {"event": "token", "data": {"delta": "你好，有问题随时问我。"}}
    yield {"event": "done", "data": {"confidence": "high", "confidence_reason": "寒暄，无需检索"}}


def _downgrade(level: str) -> str:
    return "mid" if level == "high" else "low"


def _heuristic_level(hits: list[Hit], settings) -> str:
    sims = [h.sim for h in hits if h.sim is not None]
    if not sims:
        return "mid"
    top = max(sims)
    if top >= settings.conf_high and len(hits) >= 2:
        return "high"
    if top >= settings.sim_threshold:
        return "mid"
    return "low"


def _citation_coverage(answer: str, hits: list[Hit]) -> tuple[float, bool]:
    cited = {int(n) for n in _CITED.findall(answer)}
    valid = {n for n in cited if 1 <= n <= len(hits)}
    coverage = min(1.0, len(valid) / min(3, len(hits))) if hits else 0.0
    return coverage, bool(valid)


async def _confidence(
    question: str, answer: str, hits: list[Hit], gateway: LLMGateway, settings
) -> tuple[str, str]:
    """三信号：检索得分分布 + 引用覆盖 + 模型自评（可用时）。取保守档位。"""
    level = _heuristic_level(hits, settings)
    reasons = []
    sims = [h.sim for h in hits if h.sim is not None]
    if sims:
        reasons.append(f"检索最高相似度 {max(sims):.2f}")
    coverage, has_valid = _citation_coverage(answer, hits)
    reasons.append(f"引用覆盖 {coverage:.0%}")
    if not has_valid and hits:
        level = _downgrade(level)
        reasons.append("回答缺少有效引用，已降档")
    if gateway.configured and settings.retrieval_mode == "adaptive":
        self_eval = await _self_eval(question, answer, gateway)
        if self_eval:
            mapping = {"充分": "high", "部分": "mid", "不足": "low"}
            model_level = mapping.get(self_eval)
            if model_level:
                reasons.append(f"模型自评：资料{self_eval}")
                if _LEVEL[model_level] < _LEVEL[level]:
                    level = model_level
    return level, "；".join(reasons)


async def _self_eval(question: str, answer: str, gateway: LLMGateway) -> str | None:
    prompt = (
        f"问题：{question}\n回答：{answer[:500]}\n"
        "仅依据回答的可靠程度回复一个词：充分 / 部分 / 不足"
    )
    try:
        text = await asyncio.wait_for(
            gateway.complete([{"role": "user", "content": prompt}]), timeout=8
        )
    except (TimeoutError, UpstreamError):
        return None
    for word in ("充分", "部分", "不足"):
        if word in text:
            return word
    return None


def _done(level: str, reason: str) -> dict:
    return {"confidence": level, "confidence_reason": reason}


def kb_is_empty(settings=None) -> bool:
    return store.is_empty(settings or get_settings())
