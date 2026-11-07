"""问题意图分类：规则优先（可解释、零延迟），供路由表选择检索策略。

类别：term 术语解释 / param 参数查询 / procedure 操作步骤 / fault 故障诊断
     / multihop 多跳关联 / chitchat 寒暄 / general 一般
"""

import re
from dataclasses import dataclass

_CHITCHAT = re.compile(
    r"^(你好|您好|hi|hello|嗨|谢谢|感谢|在吗|再见|早上好|下午好|晚上好)\b", re.IGNORECASE
)
_FAULT = re.compile(
    r"报错|故障|错误码|错误代码|异常|报警|警报|fault code|error code", re.IGNORECASE
)
_FAULT_CODE = re.compile(r"\b[Ee][-]?\d{2,3}\b")
_PARAM = re.compile(
    r"多少|额定|参数表?|周期|规格|范围|容量|流量|压力值|温度值|功率是|尺寸|重量|电压|电流|几 "
)
_TERM = re.compile(
    r"是什么|什么意思|什么含义|什么定义|定义是|指什么|指的是|全称|缩写|同一|一回事|一样吗|等同|区别|difference|what is|what does.*mean",
    re.IGNORECASE,
)
_PROCEDURE = re.compile(
    r"怎么|如何|步骤|操作流程|操作方法|处理|处置|怎么办|怎么排查|怎么处理|维护流程|how to|how do",
    re.IGNORECASE,
)
_ENTITY = re.compile(r"[A-Za-z][A-Za-z0-9.\-]{1,14}")
_CONNECTIVE = re.compile(r"和|与|跟|以及|分别|两者|两个都|都是")
_STOPWORDS = {
    "what",
    "is",
    "the",
    "a",
    "an",
    "of",
    "in",
    "for",
    "and",
    "how",
    "to",
    "do",
    "does",
    "mean",
    "are",
    "同一",
}
# “平均无故障时间”等可靠性术语中的“故障”不是故障诊断意图，匹配前剔除
_NO_FAULT_TERM = re.compile(r"(平均)?无故障(运行)?时间?")


@dataclass
class IntentResult:
    intent: str
    entities: list[str]


def entities_of(question: str) -> list[str]:
    """提取问题中的实体信号（拉丁缩写/型号/编码），多跳判断与子查询生成共用。"""
    seen = []
    for tok in _ENTITY.findall(question):
        low = tok.lower().rstrip(".-")
        if len(low) >= 2 and low not in _STOPWORDS and low not in seen:
            seen.append(low)
    return seen


def classify(question: str) -> IntentResult:
    q = question.strip()
    entities = entities_of(q)
    if len(q) <= 20 and _CHITCHAT.search(q):
        return IntentResult("chitchat", entities)
    fault_text = _NO_FAULT_TERM.sub("", q)
    if _FAULT.search(fault_text) or _FAULT_CODE.search(q):
        return IntentResult("fault", entities)
    # 多跳：连接词 + ≥2 实体，或“对比型术语问题”
    if (_CONNECTIVE.search(q) and len(entities) >= 2) or (
        _TERM.search(q) and _CONNECTIVE.search(q)
    ):
        return IntentResult("multihop", entities)
    if _PARAM.search(q):
        return IntentResult("param", entities)
    if _TERM.search(q):
        return IntentResult("term", entities)
    if _PROCEDURE.search(q):
        return IntentResult("procedure", entities)
    return IntentResult("general", entities)
