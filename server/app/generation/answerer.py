"""可信生成：仅依据检索资料作答，引用角标 [n]，资料不足时明确拒答。

未配置模型网关时降级为原文摘录（开头注明），保证离线开发与测试链路完整。
"""

from collections.abc import AsyncIterator

from app.core.config import Settings
from app.core.llm import LLMGateway
from app.retrieval.searcher import Hit

SYSTEM_PROMPT = (
    "你是企业知识库问答助手。规则："
    "1) 只能依据下方【资料】回答，不得引入资料外的知识；"
    "2) 每个事实后用引用编号标注，如 [1]、[2]；"
    "3) 资料不足以回答时，明确说“现有资料未覆盖该问题”，并说明已检索到的相关范围；"
    "4) 语言简洁，与提问语言一致。"
)

REFUSAL_TEMPLATE = (
    "现有资料未覆盖该问题。已检索范围：{sections}。"
    "可补充相关文档，或在问题中给出更具体的型号、编号或术语后重试。"
)


def build_context(hits: list[Hit]) -> str:
    blocks = []
    for i, h in enumerate(hits, start=1):
        loc = f"{h.doc_name} · {h.section}"
        if h.page:
            loc += f" · 第{h.page}页"
        if h.table_no:
            loc += f" · {h.table_no}"
        blocks.append(f"[{i}] {loc}\n{h.text}")
    return "\n\n".join(blocks)


def confidence_of(hits: list[Hit], refused: bool, settings: Settings) -> tuple[str, str]:
    if refused or not hits:
        return "low", "检索无有效命中，答案为拒答说明"
    sims = [h.sim for h in hits if h.sim is not None]
    if not sims:
        return "mid", f"关键词检索命中 {len(hits)} 条，语义相似度未知"
    top = max(sims)
    if top >= settings.conf_high and len(hits) >= 2:
        return "high", f"最高相似度 {top:.2f}，命中 {len(hits)} 条，多来源相互印证"
    if top >= settings.sim_threshold:
        return "mid", f"最高相似度 {top:.2f}，命中 {len(hits)} 条"
    return "low", "命中相似度偏低，答案可靠性有限"


async def stream_answer(
    question: str, hits: list[Hit], gateway: LLMGateway | None
) -> AsyncIterator[str]:
    if gateway and gateway.configured:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"【资料】\n{build_context(hits)}\n\n【问题】\n{question}",
            },
        ]
        async for delta in gateway.stream(messages):
            yield delta
        return

    # 降级路径：原文摘录（离线开发/测试用，前端可正常走通全链路）
    # 表格块信息密度高且行内即答案，不截断；文本块取前 200 字符
    lines = ["（未配置生成模型，以下为检索原文摘录）"]
    for i, h in enumerate(hits[:3], start=1):
        limit = 800 if h.kind == "table" else 200
        excerpt = h.text[:limit].replace("\n", " ")
        lines.append(f"{excerpt}… [{i}]")
    yield "\n".join(lines)
