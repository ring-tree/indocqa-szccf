"""token 计量：默认启发式保守估算，离线可用；接口可替换为真实分词器。"""

import math
import re
from functools import lru_cache
from typing import Protocol

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*")
_NUM = re.compile(r"\d+(?:\.\d+)?")

DEFAULT_MARGIN = 1.08
MESSAGE_OVERHEAD = 4


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...

    def count_message(self, role: str, content: str) -> int: ...


class HeuristicTokenCounter:
    """偏保守的字符级估算：CJK 1 token/字，拉丁词 1.3，数字串 1，其余 0.5。

    margin 默认 1.08，使估算值不低于常见 BPE 分词结果，再配合预算安全系数
    形成“双保险”，保证不超出模型窗口。
    """

    def __init__(self, margin: float = DEFAULT_MARGIN):
        self.margin = max(1.0, float(margin))

    def count(self, text: str) -> int:
        return _count_cached(text or "", self.margin)

    def count_message(self, role: str, content: str) -> int:
        return self.count(content) + MESSAGE_OVERHEAD


@lru_cache(maxsize=4096)
def _count_cached(text: str, margin: float) -> int:
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    latin_words = len(_LATIN.findall(text))
    numbers = len(_NUM.findall(text))
    consumed = (
        cjk + sum(len(m) for m in _LATIN.findall(text)) + sum(len(m) for m in _NUM.findall(text))
    )
    other = max(0, len(text) - consumed)
    raw = cjk * 1.0 + latin_words * 1.3 + numbers * 1.0 + other * 0.5
    return max(1, math.ceil(raw * margin))


@lru_cache(maxsize=8)
def _counter_for(margin: float) -> HeuristicTokenCounter:
    return HeuristicTokenCounter(margin)


def get_counter(settings) -> TokenCounter:
    return _counter_for(float(getattr(settings, "context_estimate_margin", DEFAULT_MARGIN)))


def estimate_tokens(text: str, margin: float = DEFAULT_MARGIN) -> int:
    return _count_cached(text or "", max(1.0, float(margin)))
