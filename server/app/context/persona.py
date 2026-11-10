"""用户画像：单租户下的会话级/租户级键值事实，规则抽取，不调用模型。"""

import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from app.context.schema import PersonaFact

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_profile (
    tenant_id  TEXT NOT NULL DEFAULT 'default',
    session_id TEXT NOT NULL DEFAULT '',
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.6,
    source     TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, session_id, key)
);
"""

_LANGUAGE = re.compile(r"以后(?:都)?(?:用|使用)(中文|英文|汉语|英语)")
_STYLE = re.compile(r"回答(?:请|尽量)?(?:简洁|简短|详细|口语|正式)")
_ROLE = re.compile(
    r"我是(?:一名|一个)?([\u4e00-\u9fffA-Za-z]{2,20}?)(工程师|技术员|主管|经理|运维|操作员)"
)
_FOCUS = re.compile(r"我(?:负责|主要(?:负责|看|关注))([^，。；\n]{2,30})")
_PREFERENCE = re.compile(r"(?:优先|尽量)(?:使用|用)([\u4e00-\u9fffA-Za-z0-9\-]{2,10})")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@contextmanager
def _connect(path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        con.executescript(_SCHEMA)
        yield con
        con.commit()
    finally:
        con.close()


def extract_persona_signals(text: str) -> list[PersonaFact]:
    facts: list[PersonaFact] = []
    match = _LANGUAGE.search(text or "")
    if match:
        value = "zh" if match.group(1) in {"中文", "汉语"} else "en"
        facts.append(PersonaFact("language", value, 0.9, "explicit"))
    match = _STYLE.search(text or "")
    if match:
        facts.append(PersonaFact("style", match.group(0)[-2:], 0.85, "explicit"))
    match = _ROLE.search(text or "")
    if match:
        facts.append(PersonaFact("role", f"{match.group(1)}{match.group(2)}", 0.8, "explicit"))
    match = _FOCUS.search(text or "")
    if match:
        facts.append(PersonaFact("focus", match.group(1).strip(), 0.75, "inferred"))
    match = _PREFERENCE.search(text or "")
    if match:
        facts.append(PersonaFact("preference", match.group(1).strip(), 0.7, "inferred"))
    return facts


def upsert_persona(settings, session_id: str, facts: list[PersonaFact]) -> None:
    if not facts:
        return
    now = _now()
    with _connect(settings.session_db) as con:
        for fact in facts:
            con.execute(
                """
                INSERT INTO user_profile(tenant_id, session_id, key, value, confidence, source, updated_at)
                VALUES('default', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, session_id, key) DO UPDATE SET
                    value=excluded.value,
                    confidence=excluded.confidence,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                WHERE excluded.confidence >= user_profile.confidence OR excluded.source='explicit'
                """,
                (session_id, fact.key, fact.value, fact.confidence, fact.source, now),
            )


def load_persona(settings, session_id: str) -> list[PersonaFact]:
    if not getattr(settings, "context_persona_enabled", True):
        return []
    with _connect(settings.session_db) as con:
        rows = con.execute(
            "SELECT * FROM user_profile WHERE tenant_id='default' AND session_id IN ('', ?)",
            (session_id,),
        ).fetchall()
    merged: dict[str, PersonaFact] = {}
    for row in rows:
        fact = PersonaFact(
            key=row["key"],
            value=row["value"],
            confidence=float(row["confidence"]),
            source=row["source"],
            session_id=row["session_id"],
            updated_at=row["updated_at"],
        )
        current = merged.get(fact.key)
        if current is None:
            merged[fact.key] = fact
            continue
        if (fact.session_id and not current.session_id) or (
            fact.session_id == current.session_id and fact.confidence >= current.confidence
        ):
            merged[fact.key] = fact
    return sorted(merged.values(), key=lambda f: f.confidence, reverse=True)
