"""LangGraph 检索编排状态图：分类 → 改写(术语扩展) → 检索 → 多跳决策(条件边)。

fixed 模式走同一张图但使用统一基线参数（不扩展、不多跳），保证对照组与
实验组共享完全相同的代码路径，差异只在策略参数——基线对比实验的前提。
"""

import re
from dataclasses import replace
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.core.config import get_settings
from app.retrieval import rerank as rerank_mod
from app.retrieval import terms as terms_mod
from app.retrieval.intents import classify
from app.retrieval.router import plan_for
from app.retrieval.searcher import Hit, search

# 故障码/步骤码/型号：用于检索时二次加权，避免代码表块压过处置步骤块
_REF_ID = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,4}[-_]?\d{1,4}(?![A-Za-z0-9])")
_ANCHOR_BOOST = 0.05


class GState(TypedDict, total=False):
    question: str
    mode: str
    folder: str | None
    folders: list | None
    top_k_override: int | None
    intent: str
    entities: list
    plan: dict
    query: str
    query_lex: str
    expansions: list
    hop: int
    sub_query: str
    hits: list
    trace: list
    need_hop: bool


def _trace(state: dict, stage: str, **detail) -> None:
    state.setdefault("trace", []).append({"stage": stage, **detail})


def classify_node(state: GState) -> dict:
    result = classify(state["question"])
    plan = plan_for(result.intent, state["mode"], get_settings())
    override = state.get("top_k_override")
    if override:
        plan = {**plan, "top_k": override, "label": f"{plan['label']} · top{override}"}
    return {
        "intent": result.intent,
        "entities": result.entities,
        "plan": plan,
        "hop": 0,
        "trace": [{"stage": "classify", "intent": result.intent, "plan": plan["label"]}],
    }


def rewrite_node(state: GState) -> dict:
    """输出两路查询：query（语义路=原问题）与 query_lex（词法路=术语扩展后）。"""
    query_lex = state["question"]
    expansions: list = []
    settings = get_settings()
    if state["plan"].get("expand") and settings.term_expansion_enabled:
        query_lex, used = terms_mod.expand_query(state["question"], terms_mod.load_clusters())
        expansions = [" / ".join(c[:3]) for c in used]
    if state.get("sub_query") and state["hop"] > 0:
        query_lex = f"{query_lex} {state['sub_query']}"
        semantic = f"{state['question']} {state['sub_query']}"
    else:
        semantic = state["question"]
    if state["intent"] in {"fault", "procedure", "param"}:
        # 故障码/步骤码在问题中重复一次，提高 BM25 查询词频：
        # “E-05 → S-12”场景下代码表会压过 S-12 处置步骤块，二次加权可把步骤块拉回候选集。
        anchors = list(dict.fromkeys(_REF_ID.findall(state["question"])))
        if anchors:
            query_lex = f"{query_lex} {' '.join(anchors)}"
    return {"query": semantic, "query_lex": query_lex, "expansions": expansions}


def retrieve_node(state: GState) -> dict:
    plan = state["plan"]
    top_k = plan.get("top_k") or 6
    if plan.get("strategy") == "none":
        return {
            "hits": [],
            "trace": state["trace"]
            + [
                {
                    "stage": "retrieve",
                    "hop": 0,
                    "strategy": "none",
                    "query": state["question"],
                    "expansions": [],
                    "hits": 0,
                }
            ],
        }
    query_sem = state.get("query") or state["question"]
    query_lex = state.get("query_lex") or state["question"]
    hits = search(
        query_sem,
        top_k=top_k * 2,
        query_lex=query_lex if query_lex != query_sem else None,
        strategy=plan.get("strategy", "hybrid"),
        folder=state.get("folder"),
        folders=state.get("folders"),
    )
    if plan.get("boost_table"):
        # 分数加权而非“表格优先”：避免低相关表格仅凭类型挤掉高相关正文
        hits = [
            replace(h, rrf=round(h.rrf * (1.12 if h.kind == "table" else 1.0), 5)) for h in hits
        ]
        hits.sort(key=lambda h: h.rrf, reverse=True)
    hits = rerank_mod.rerank(query_lex, hits, top_k)
    # 显式故障码/步骤码/型号锚点补查：代码表块可能压过处置步骤块，
    # 用锚点本身再做一次关键词检索，并把命中并入候选集。
    anchors = list(dict.fromkeys(_REF_ID.findall(state["question"])))
    anchor_hits = []
    if anchors and state.get("intent") in {"fault", "procedure", "param"}:
        anchor_query = " ".join(anchors)
        if state["intent"] == "fault":
            anchor_query += " 处置步骤"
        elif state["intent"] == "procedure":
            anchor_query += " 操作步骤"
        anchor_hits = search(
            anchor_query,
            top_k=4,
            strategy="keyword",
            folder=state.get("folder"),
            folders=state.get("folders"),
        )
        # 锚点命中必须进入最终候选：RRF 分数来自独立检索，直接比较会输给主检索
        anchor_hits = [
            replace(hit, rrf=round(hit.rrf + _ANCHOR_BOOST, 5)) for hit in anchor_hits
        ]
    merged: dict[str, Hit] = {}
    for hit in [*anchor_hits, *state.get("hits", []), *hits]:
        current = merged.get(hit.chunk_id)
        if current is None or hit.rrf > current.rrf:
            merged[hit.chunk_id] = hit
    # 故障/步骤/参数问题经常需要同一手册的多个相邻块（代码表 + 处置步骤 + 周期表），
    # 放宽单文档席位；多跳/一般问题仍保持跨文档多样化。
    per_doc_limit = 4 if state.get("intent") in {"fault", "procedure", "param"} else 2
    final = _diversify(
        list(merged.values()),
        top_k if not plan.get("multihop") else max(top_k, 6),
        per_doc_limit=per_doc_limit,
    )
    return {
        "hits": final,
        "trace": state["trace"]
        + [
            {
                "stage": "retrieve",
                "hop": state["hop"],
                "strategy": plan["strategy"],
                "query": query_lex,
                "anchors": anchors,
                "expansions": state.get("expansions", []),
                "folder": state.get("folder") or "",
                "folders": state.get("folders") or [],
                "hits": len(final),
            }
        ],
    }


def _diversify(hits: list, top_k: int, per_doc_limit: int = 2) -> list:
    """跨文档多样化：默认单文档最多占 2 席，避免多跳问题命中偏向单一文档。"""
    hits = sorted(hits, key=lambda h: h.rrf, reverse=True)
    out, per_doc = [], {}
    for h in hits:
        used = per_doc.get(h.doc_id, 0)
        if used >= per_doc_limit:
            continue
        per_doc[h.doc_id] = used + 1
        out.append(h)
        if len(out) >= top_k:
            return out
    for h in hits:  # 席位未满时回填
        if h not in out:
            out.append(h)
            if len(out) >= top_k:
                break
    return out


def decide_node(state: GState) -> dict:
    """多跳决策：还有未覆盖实体且未达跳数上限 → 再检索一轮（Retrieve-Read-Retrieve）。

    实体 = 拉丁 token（分类器抽取）+ 问题中命中的术语簇成员（补全中文实体）。
    """
    plan = state["plan"]
    if not plan.get("multihop") or state["hop"] >= plan.get("max_hops", 2):
        return {
            "need_hop": False,
            "trace": state["trace"] + [{"stage": "decide", "result": "收敛，结束检索"}],
        }
    entities = list(state.get("entities", []))
    low_q = state["question"].lower()
    for cluster in terms_mod.load_clusters():
        entities.extend(m for m in cluster if len(m) >= 3 and m.lower() in low_q)
    covered = " ".join(h.text for h in state["hits"]).lower()
    missing = [e for e in dict.fromkeys(entities) if e.lower() not in covered]
    docs = {h.doc_name for h in state["hits"]}
    if not missing:
        if len(entities) >= 2 and len(docs) < 2:
            # 多实体问题只命中单一文档：补查一轮，尝试带入其他文档的定义块
            missing = entities[:2]
        else:
            return {
                "need_hop": False,
                "trace": state["trace"] + [{"stage": "decide", "result": "实体已全覆盖，结束检索"}],
            }
    sub_query = " ".join(missing[:3])
    return {
        "need_hop": True,
        "sub_query": sub_query,
        "hop": state["hop"] + 1,
        "trace": state["trace"]
        + [{"stage": "decide", "result": f"第 {state['hop'] + 1} 跳，补查实体: {sub_query}"}],
    }


def _route(state: GState) -> str:
    return "rewrite" if state.get("need_hop") else "end"


def build_graph():
    g = StateGraph(GState)
    g.add_node("classify", classify_node)
    g.add_node("rewrite", rewrite_node)
    g.add_node("retrieve", retrieve_node)
    g.add_node("decide", decide_node)
    g.add_edge(START, "classify")
    g.add_edge("classify", "rewrite")
    g.add_edge("rewrite", "retrieve")
    g.add_edge("retrieve", "decide")
    g.add_conditional_edges("decide", _route, {"rewrite": "rewrite", "end": END})
    return g.compile()


_compiled = None


def get_compiled_graph():
    global _compiled
    if _compiled is None:
        _compiled = build_graph()
    return _compiled


async def run_retrieval(
    question: str,
    mode: str,
    settings=None,
    top_k: int | None = None,
    folder: str | None = None,
    folders: list[str] | None = None,
) -> dict:
    """执行状态图，返回最终状态（plan / intent / hits / trace）。"""
    final = await get_compiled_graph().ainvoke(
        {
            "question": question,
            "mode": mode,
            "folder": folder,
            "folders": folders,
            "top_k_override": top_k,
        }
    )
    hits = [h for h in final.get("hits", []) if isinstance(h, Hit)]
    return {
        "intent": final.get("intent", "general"),
        "entities": final.get("entities", []),
        "plan": final.get("plan", {}),
        "query": final.get("query", question),
        "hits": hits,
        "trace": final.get("trace", []),
    }
