"""生成层安全过滤：违规内容拦截、PII/凭据脱敏与提示词泄漏检测。"""

import re
from dataclasses import dataclass, field

SAFETY_REFUSAL = "该内容不符合安全规范，已拦截。请调整问题后重试。"


@dataclass(frozen=True)
class SafetyRule:
    rule_id: str
    pattern: re.Pattern
    category: str
    severity: str


@dataclass
class SafetyHit:
    rule_id: str
    category: str
    severity: str
    term: str

    def to_dict(self) -> dict:
        return {
            "ruleId": self.rule_id,
            "category": self.category,
            "severity": self.severity,
        }


@dataclass
class SafetyResult:
    text: str
    hits: list[SafetyHit] = field(default_factory=list)
    blocked: bool = False

    def to_dict(self) -> dict:
        return {
            "blocked": self.blocked,
            "hits": [hit.to_dict() for hit in self.hits],
            "categories": sorted({hit.category for hit in self.hits}),
        }


_BLOCK_RULES = (
    SafetyRule(
        "S1", re.compile(r"色情|裸聊|性服务|成人视频|porn", re.IGNORECASE), "sexual", "high"
    ),
    SafetyRule(
        "S2",
        re.compile(r"制作炸弹|自制炸药|恐怖袭击|枪杀|虐杀|爆炸物配方"),
        "violence",
        "high",
    ),
    SafetyRule(
        "S3",
        re.compile(r"冰毒|海洛因|吸毒方法|毒品交易|制毒"),
        "drugs",
        "high",
    ),
    SafetyRule("S4", re.compile(r"自杀方法|怎么自杀|自残教程|轻生方法"), "self_harm", "high"),
    SafetyRule("S5", re.compile(r"种族清洗|煽动仇恨|灭绝.{0,4}人"), "hate", "high"),
)

_REDACT_RULES = (
    SafetyRule("P1", re.compile(r"\b\d{17}[\dXx]\b"), "pii", "medium"),
    SafetyRule("P2", re.compile(r"\b1[3-9]\d{9}\b"), "pii", "medium"),
    SafetyRule("P3", re.compile(r"\b\d{16,19}\b"), "pii", "medium"),
    SafetyRule("P4", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "pii", "low"),
    SafetyRule("P5", re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"), "credential", "high"),
    SafetyRule(
        "P6",
        re.compile(r"(?i)\b(?:password|passwd|api[_-]?key|token)\s*[:=]\s*\S+"),
        "credential",
        "high",
    ),
    SafetyRule(
        "P7",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]+?-----END"),
        "credential",
        "high",
    ),
)

_LEAK_RULE = SafetyRule(
    "L1",
    re.compile(r"(?i)system\s+prompt|系统提示词|系统指令"),
    "prompt_leak",
    "medium",
)


class SafetyGuard:
    """对输入、检索上下文与输出来源统一做检测与脱敏。"""

    def inspect(self, text: str, block: bool = True) -> SafetyResult:
        source = text or ""
        hits: list[SafetyHit] = []
        ranges: list[tuple[int, int]] = []
        for rule in (*_BLOCK_RULES, *_REDACT_RULES, _LEAK_RULE):
            for match in rule.pattern.finditer(source):
                hits.append(SafetyHit(rule.rule_id, rule.category, rule.severity, match.group(0)))
                if rule in _REDACT_RULES or rule is _LEAK_RULE:
                    ranges.append((match.start(), match.end()))
        blocked = block and any(
            hit.severity == "high"
            and hit.category in {"sexual", "violence", "drugs", "self_harm", "hate"}
            for hit in hits
        )
        cleaned = source
        for start, end in sorted(ranges, reverse=True):
            cleaned = f"{cleaned[:start]}***{cleaned[end:]}"
        return SafetyResult(cleaned, hits, blocked)

    def redact(self, text: str) -> str:
        return self.inspect(text, block=False).text

    def sanitize_context(self, text: str) -> SafetyResult:
        return self.inspect(text, block=True)

    def sanitize_output(self, text: str) -> SafetyResult:
        return self.inspect(text, block=True)
