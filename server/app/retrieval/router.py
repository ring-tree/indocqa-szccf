"""检索策略路由表：意图 → 检索方式与参数。fixed 模式走统一基线参数。"""

from app.core.config import Settings

# adaptive 模式策略表；fixed 模式使用 DEFAULT_PLAN（对照组参数）
STRATEGIES = {
    "term": {
        "strategy": "hybrid",
        "top_k": 6,
        "expand": True,
        "boost_table": False,
        "multihop": False,
    },
    "param": {
        "strategy": "hybrid",
        "top_k": 6,
        "expand": True,
        "boost_table": True,
        "multihop": False,
    },
    "procedure": {
        "strategy": "hybrid",
        "top_k": 6,
        "expand": False,
        "boost_table": False,
        "multihop": False,
    },
    "fault": {
        "strategy": "keyword",
        "top_k": 8,
        "expand": True,
        "boost_table": True,
        "multihop": False,
    },
    "multihop": {
        "strategy": "hybrid",
        "top_k": 6,
        "expand": True,
        "boost_table": False,
        "multihop": True,
        "max_hops": 2,
    },
    "general": {
        "strategy": "hybrid",
        "top_k": 6,
        "expand": False,
        "boost_table": False,
        "multihop": False,
    },
    "chitchat": {"strategy": "none", "skip_retrieval": True},
}
DEFAULT_PLAN = {
    "strategy": "hybrid",
    "top_k": None,
    "expand": False,
    "boost_table": False,
    "multihop": False,
}


def plan_for(intent: str, mode: str, settings: Settings) -> dict:
    if mode != "adaptive":
        plan = dict(DEFAULT_PLAN)
        plan["top_k"] = settings.top_k_default
        plan["label"] = "fixed 基线（统一混合检索参数）"
        return plan
    plan = dict(STRATEGIES.get(intent, STRATEGIES["general"]))
    plan.setdefault("max_hops", 2)
    plan["label"] = f"adaptive · {intent} → {plan['strategy']}"
    return plan
