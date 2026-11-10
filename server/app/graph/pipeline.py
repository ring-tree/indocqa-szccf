"""问答编排：LangGraph 检索状态图 + 流式生成 + 会话上下文 + 事后置信度。

SSE 事件顺序：trace* → sources → token* → done（错误时以 error 事件收尾）。
"""

import asyncio
import re
from collections.abc import AsyncIterator

from app.context import AssemblyRequest, ContextAssembler
from app.context.errors import ContextError
from app.context.persona import extract_persona_signals, load_persona, upsert_persona
from app.context.schema import ContextBundle, TaskState
from app.core.config import get_settings
from app.core.exceptions import UpstreamError
from app.core.llm import LLMGateway
from app.generation.answerer import REFUSAL_TEMPLATE, stream_answer
from app.graph.graph import run_retrieval
from app.indexing import store
from app.indexing.manifest import Manifest
from app.retrieval.searcher import Hit
from app.session import store as session_store

_LEVEL = {"low": 0, "mid": 1, "high": 2}
_CITED = re.compile(r"\[(\d+)\]")
_REFER = re.compile(r"它|这个|那个|该|其|上面|刚才|前面|这两|这三")
_REFUSAL_HEAD = re.compile(
    r"^(?:现有)?资料(?:中)?(?:并)?未(?:覆盖|涉及|提及)|^无法(?:根据|回答)|^(?:很)?抱歉|^没有找到"
)
_BOUNDARY_LABEL = "【非知识库内容】\n"
_LABEL_ECHO = re.compile(r"^\s*(?:\*{0,2}(?:【\s*非知识库内容\s*】|非知识库内容)[:：]?\*{0,2}\s*)+")
_CHITCHAT_SYSTEM = (
    "你是企业知识库助手的门面。对寒暄给出简短友好的回应，一两句话即可，不涉及任何技术内容。"
)
_DIRECT_SYSTEM = (
    "你是工阅 IndocQA 的智能助手。回答简洁、准确，与用户语言一致；不确定时明确说明，不要编造。"
)
_GENERAL_SYSTEM = (
    "你是工阅 IndocQA 的智能助手。当前企业知识库没有覆盖用户问题。"
    "请用通用知识回答，回答开头已由系统标注【非知识库内容】，不要重复标注；"
    "不得编造引用编号，也不要声称内容来自知识库。"
)


def _refusal_head(text: str) -> bool:
    """只识别“整段拒答”的开头；正文中途说某细节未覆盖不应触发通用回答。"""
    clean = text.lstrip(" \n*#>-")
    return bool(_REFUSAL_HEAD.search(clean[:60]))


def _source_item(hit: Hit, idx: int) -> dict:
    limit = 400 if hit.kind == "table" else 240
    return {
        "index": idx,
        "docId": hit.doc_id,
        "docName": hit.doc_name,
        "section": hit.section,
        "page": hit.page,
        "locator": hit.table_no,
        "folder": hit.folder,
        "score": round(hit.sim, 4) if hit.sim is not None else None,
        "snippet": hit.text[:limit],
    }


def _done(
    level: str,
    reason: str,
    session_id: str | None = None,
    message_id: int | None = None,
    boundary: str | None = None,
    scope: list[str] | None = None,
    model: str | None = None,
) -> dict:
    data = {
        "confidence": level,
        "confidence_reason": reason,
        "sessionId": session_id,
        "messageId": message_id,
        "boundary": boundary,
        "model": model,
    }
    if scope is not None:
        data["searched_scope"] = scope
    return data


def _history_rows(session_id: str, settings) -> list[dict]:
    return session_store.get_messages(
        settings.session_db, session_id, limit=settings.session_history_turns * 2
    )


def _history(rows: list[dict]) -> list[dict]:
    return [{"role": row["role"], "content": row["content"]} for row in rows]


def _task_state(settings, session_id: str) -> TaskState:
    return TaskState.from_dict(
        session_id, session_store.get_task_state(settings.session_db, session_id)
    )


def _assemble(
    message: str,
    retrieval: dict,
    rows: list[dict],
    settings,
    session_id: str,
    model: str | None,
    folders: list[str] | None,
) -> ContextBundle:
    upsert_persona(settings, session_id, extract_persona_signals(message))
    persona = load_persona(settings, session_id)
    task = _task_state(settings, session_id)
    request = AssemblyRequest(
        session_id=session_id,
        question=message,
        intent=retrieval["intent"],
        hits=retrieval["hits"],
        history=rows,
        mode="kb",
        folders=list(folders or []),
        model=model,
        entities=list(retrieval.get("entities") or []),
        task_state=task,
        persona=persona,
        template_id=settings.context_template_id,
        template_version=settings.context_template_version,
    )
    return ContextAssembler(settings).assemble(request)


def _context_query(message: str, history: list[dict]) -> str:
    """短问句或含指代词时补上一轮用户问题，避免“它的额定值”这类追问丢失主语。"""
    last_user = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    if last_user and last_user != message and (len(message) < 12 or _REFER.search(message)):
        return f"{last_user} {message}"
    return message


def _scope(settings, folder: str | None = None, folders: list[str] | None = None) -> list[str]:
    """已入库文档名，用于库外回答说明“检索过什么”；可按目录收敛。"""
    try:
        docs = Manifest(Manifest.path_for(settings)).documents()
    except (OSError, ValueError, KeyError):
        return []
    scopes = [f for f in ([folder] if folder else []) + list(folders or []) if f]
    names = []
    for info in docs.values():
        doc_folder = info.get("folder") or ""
        if scopes and not any(
            doc_folder == scope or doc_folder.startswith(scope + "/") for scope in scopes
        ):
            continue
        if info.get("filename"):
            names.append(info["filename"])
    return names[:10]


def _boundary_min_sim(settings) -> float:
    """弱命中阈值按嵌入后端分档：本地小模型的相似度分布整体低于 bge-m3。"""
    from app.indexing.embedder import get_embedder

    if get_embedder(settings).backend_id.startswith("ollama"):
        return settings.boundary_min_sim
    return settings.boundary_min_sim_local


def _weak_hits(hits: list[Hit], settings) -> bool:
    """向量相似度整体偏低时视为弱命中；纯关键词命中（sim=None）不参与判断。"""
    sims = [h.sim for h in hits if h.sim is not None]
    return bool(sims) and max(sims) < _boundary_min_sim(settings)


async def chat_stream(
    message: str,
    mode: str,
    top_k: int | None = None,
    settings=None,
    session_id: str | None = None,
    profile: str | None = None,
    folder: str | None = None,
    folders: list[str] | None = None,
) -> AsyncIterator[dict]:
    settings = settings or get_settings()
    gateway = LLMGateway(settings, profile)

    session = session_store.ensure_session(settings.session_db, session_id, message)
    sid = session["id"]
    rows = _history_rows(sid, settings)
    history = _history(rows)
    session_store.append_message(settings.session_db, sid, "user", message)
    retrieval_query = _context_query(message, history)

    if mode == "direct":
        if not gateway.configured:
            raise UpstreamError("直连模式需要配置模型网关（server/.env）")
        parts: list[str] = []
        try:
            async for delta in gateway.stream(
                [
                    {"role": "system", "content": _DIRECT_SYSTEM},
                    *history,
                    {"role": "user", "content": message},
                ]
            ):
                parts.append(delta)
                yield {"event": "token", "data": {"delta": delta}}
        except UpstreamError as e:
            yield {"event": "error", "data": {"code": e.code, "message": e.message}}
            return
        mid = session_store.append_message(
            settings.session_db, sid, "assistant", "".join(parts), model=gateway.last_model
        )
        yield {"event": "done", "data": _done(None, "", sid, mid, model=gateway.last_model)}
        return

    retrieval = await run_retrieval(
        retrieval_query,
        settings.retrieval_mode,
        settings,
        top_k,
        folder=folder,
        folders=folders,
    )
    for step in retrieval["trace"]:
        yield {"event": "trace", "data": step}

    plan = retrieval["plan"]
    hits = retrieval["hits"]
    scope = _scope(settings, folder, folders)

    if plan.get("skip_retrieval"):
        parts = []
        async for ev in _chitchat_reply(message, gateway, history):
            parts.append(ev["data"]["delta"])
            yield ev
        mid = session_store.append_message(
            settings.session_db,
            sid,
            "assistant",
            "".join(parts),
            trace=retrieval["trace"],
            confidence="high",
            confidence_reason="寒暄，无需检索",
            model=gateway.last_model,
        )
        yield {
            "event": "done",
            "data": _done("high", "寒暄，无需检索", sid, mid, model=gateway.last_model),
        }
        return

    weak = _weak_hits(hits, settings)

    if not hits or weak:
        yield {"event": "sources", "data": {"items": []}}
        reason = "检索相似度不足，按库外问题处理" if weak else "无有效命中，按库外问题处理"
        retrieval["trace"] = retrieval["trace"] + [{"stage": "boundary", "result": reason}]
        yield {"event": "trace", "data": retrieval["trace"][-1]}
        async for ev in _boundary_reply(
            message, gateway, settings, sid, retrieval["trace"], scope, history, hits
        ):
            yield ev
        return

    bundle = None
    answer_hits = hits
    context_on = settings.context_enabled and settings.context_mode in {"on", "shadow"}
    if context_on:
        try:
            bundle = _assemble(
                message,
                retrieval,
                rows,
                settings,
                sid,
                settings.llm_model or None,
                folders,
            )
            yield {
                "event": "context",
                "data": {
                    **bundle.stats.to_dict(),
                    "template": f"{bundle.template_id}@{bundle.template_version}",
                    "warnings": bundle.warnings,
                    "shadow": settings.context_mode == "shadow",
                },
            }
            if bundle.task_state is not None:
                session_store.save_task_state(settings.session_db, sid, bundle.task_state.to_dict())
            if settings.context_mode == "on":
                answer_hits = bundle.evidence_hits
                if not answer_hits:
                    yield {"event": "sources", "data": {"items": []}}
                    reason = "上下文组装后无有效资料，按库外问题处理"
                    retrieval["trace"] = retrieval["trace"] + [
                        {"stage": "boundary", "result": reason}
                    ]
                    yield {"event": "trace", "data": retrieval["trace"][-1]}
                    async for ev in _boundary_reply(
                        message, gateway, settings, sid, retrieval["trace"], scope, history, hits
                    ):
                        yield ev
                    return
        except ContextError as e:
            bundle = None
            answer_hits = hits
            yield {
                "event": "trace",
                "data": {"stage": "context", "result": f"组装降级：{e.code}"},
            }

    yield {
        "event": "sources",
        "data": {"items": [_source_item(h, i) for i, h in enumerate(answer_hits, 1)]},
    }

    answer_parts: list[str] = []
    prefix = ""
    started = False
    try:
        async for delta in stream_answer(
            message,
            answer_hits,
            gateway,
            history,
            retrieval["intent"],
            bundle=bundle if settings.context_mode == "on" else None,
        ):
            if not started:
                prefix += delta
                if _refusal_head(prefix):
                    # 命中的块与问题无关、模型整段拒答：清空引用并转通用知识回答
                    yield {"event": "sources", "data": {"items": []}}
                    yield {
                        "event": "trace",
                        "data": {"stage": "boundary", "result": "资料未覆盖，转通用知识回答"},
                    }
                    async for ev in _boundary_reply(
                        message,
                        gateway,
                        settings,
                        sid,
                        retrieval["trace"],
                        scope,
                        history,
                        answer_hits,
                    ):
                        yield ev
                    return
                if len(prefix) < 40:
                    continue
                answer_parts.append(prefix)
                yield {"event": "token", "data": {"delta": prefix}}
                started = True
                continue
            answer_parts.append(delta)
            yield {"event": "token", "data": {"delta": delta}}
    except UpstreamError as e:
        yield {"event": "error", "data": {"code": e.code, "message": e.message}}
        return
    if not started and prefix:
        answer_parts.append(prefix)
        yield {"event": "token", "data": {"delta": prefix}}

    answer = "".join(answer_parts)
    _cov, has_valid_citation = _citation_coverage(answer, answer_hits)
    if answer_hits and not has_valid_citation:
        # 引用补正：回答缺少有效引用时带强指令重生成一次（可从 trace 观察）
        yield {
            "event": "trace",
            "data": {"stage": "citation_retry", "result": "回答缺少有效引用，重生成补正"},
        }
        answer_parts = []
        try:
            yield {"event": "token", "data": {"delta": "\n——引用补正——\n"}}
            async for delta in stream_answer(
                message,
                answer_hits,
                gateway,
                history,
                retrieval["intent"],
                bundle=bundle if settings.context_mode == "on" else None,
                extra_instruction=(
                    "（必须做到：每个事实句末标注引用编号，如 [1] [2]，编号对应资料标号）"
                ),
            ):
                answer_parts.append(delta)
                yield {"event": "token", "data": {"delta": delta}}
        except UpstreamError:
            pass  # 补正失败保留原答案
        if answer_parts:
            answer = "".join(answer_parts)

    answer_model = gateway.last_model  # 自评会再次调用网关，先记住回答实际使用的模型
    level, reason = await _confidence(message, answer, answer_hits, gateway, settings)
    mid = session_store.append_message(
        settings.session_db,
        sid,
        "assistant",
        answer,
        sources=[_source_item(h, i) for i, h in enumerate(answer_hits, 1)],
        trace=retrieval["trace"],
        confidence=level,
        confidence_reason=reason,
        model=answer_model,
        context_manifest=(
            bundle.manifest.to_dict()
            if bundle is not None
            and settings.context_mode == "on"
            and settings.context_trace_persist
            else None
        ),
    )
    yield {
        "event": "done",
        "data": _done(level, reason, sid, mid, None, scope, model=answer_model),
    }


async def _boundary_reply(
    message: str,
    gateway: LLMGateway,
    settings,
    session_id: str,
    trace: list,
    scope: list[str],
    history: list[dict] | None = None,
    weak_hits: list[Hit] | None = None,
) -> AsyncIterator[dict]:
    """库外问题：有生成模型则用通用知识回答并标注；无模型则拒答并说明检索范围。"""
    parts: list[str] = []
    boundary = "general"
    reason = "知识库未覆盖，已用通用知识回答"
    if gateway.configured:
        parts.append(_BOUNDARY_LABEL)
        yield {"event": "token", "data": {"delta": _BOUNDARY_LABEL}}
        try:
            first = True
            buf = ""
            async for delta in gateway.stream(
                [
                    {"role": "system", "content": _GENERAL_SYSTEM},
                    *(history or []),
                    {"role": "user", "content": message},
                ]
            ):
                if first:
                    buf += delta
                    if len(buf) < 20:
                        continue
                    cleaned = _LABEL_ECHO.sub("", buf)
                    if cleaned:
                        parts.append(cleaned)
                        yield {"event": "token", "data": {"delta": cleaned}}
                    first = False
                    continue
                parts.append(delta)
                yield {"event": "token", "data": {"delta": delta}}
            if first and buf:
                cleaned = _LABEL_ECHO.sub("", buf)
                if cleaned:
                    parts.append(cleaned)
                    yield {"event": "token", "data": {"delta": cleaned}}
        except UpstreamError:
            fallback = REFUSAL_TEMPLATE.format(sections="、".join(scope) or "知识库")
            parts = [fallback]
            yield {"event": "token", "data": {"delta": fallback}}
            boundary, reason = "refused", "通用回答失败，已回退拒答"
    else:
        fallback = REFUSAL_TEMPLATE.format(sections="、".join(scope) or "知识库")
        parts = [fallback]
        yield {"event": "token", "data": {"delta": fallback}}
        boundary, reason = "refused", "检索无有效命中，答案为拒答说明"

    if weak_hits:
        hint = "\n\n可能相关的章节：" + "；".join(
            f"{h.doc_name} · {h.section}" for h in weak_hits[:3]
        )
        parts.append(hint)
        yield {"event": "token", "data": {"delta": hint}}

    mid = session_store.append_message(
        settings.session_db,
        session_id,
        "assistant",
        "".join(parts),
        trace=trace,
        confidence="low",
        confidence_reason=reason,
        boundary=boundary,
        model=gateway.last_model if boundary == "general" else None,
    )
    yield {
        "event": "done",
        "data": _done(
            "low",
            reason,
            session_id,
            mid,
            boundary,
            scope,
            model=gateway.last_model if boundary == "general" else None,
        ),
    }


async def _chitchat_reply(
    message: str, gateway: LLMGateway, history: list[dict]
) -> AsyncIterator[dict]:
    if gateway.configured:
        try:
            async for delta in gateway.stream(
                [
                    {"role": "system", "content": _CHITCHAT_SYSTEM},
                    *history,
                    {"role": "user", "content": message},
                ]
            ):
                yield {"event": "token", "data": {"delta": delta}}
            return
        except UpstreamError:
            pass
    yield {"event": "token", "data": {"delta": "你好，有问题随时问我。"}}


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
    # 有有效引用且检索置信度高时，不允许自评直接把结果打到 low；
    # 纯关键词精确命中（sim=None）时同样保底 mid，避免故障码类正确回答被判低置信
    keyword_only = bool(hits) and not sims
    floor = (
        "mid"
        if has_valid and (keyword_only or (sims and max(sims) >= settings.conf_high))
        else "low"
    )
    if gateway.configured and settings.retrieval_mode == "adaptive":
        self_eval = await _self_eval(question, answer, gateway)
        if self_eval:
            mapping = {"充分": "high", "部分": "mid", "不足": "low"}
            model_level = mapping.get(self_eval)
            if model_level and _LEVEL[model_level] < _LEVEL[level]:
                candidate = _downgrade(level) if model_level == "low" else model_level
                if _LEVEL[candidate] < _LEVEL[floor]:
                    candidate = floor
                if candidate != level:
                    reasons.append(f"模型自评：资料{self_eval}（至多降一档）")
                    level = candidate
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


def kb_is_empty(settings=None) -> bool:
    return store.is_empty(settings or get_settings())
