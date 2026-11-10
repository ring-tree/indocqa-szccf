"""会话任务状态：规则驱动的主题、实体、检索范围与轮次转移，不调用模型。"""

import re

from app.context.schema import TaskState, TurnSignal, now_iso

_REFER = re.compile(r"它|这个|那个|该|其|上面|刚才|前面|这两|这三")
_MAX_ENTITIES = 12


def _merge_entities(previous: list[str], current: list[str]) -> list[str]:
    out: list[str] = []
    seen = set()
    for item in [*previous, *current]:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item.strip())
    return out[-_MAX_ENTITIES:]


def _topic(previous: str, question: str) -> str:
    clean = " ".join((question or "").split())
    if previous and (len(clean) < 12 or _REFER.search(clean)):
        return previous
    return clean[:40] or previous


def update_task_state(previous: TaskState | None, signal: TurnSignal, session_id: str) -> TaskState:
    previous = previous or TaskState(session_id=session_id)
    status = previous.status or "idle"
    if signal.intent in {"chitchat", "general"}:
        status = status if status != "idle" else "idle"
    else:
        status = "answering"
    return TaskState(
        session_id=session_id,
        version=previous.version + 1,
        status=status,
        topic=_topic(previous.topic, signal.question),
        entities=_merge_entities(previous.entities, signal.entities),
        scope_folders=list(signal.folders or previous.scope_folders)[:8],
        last_intent=signal.intent or previous.last_intent,
        pending=[],
        turn=previous.turn + 1,
        updated_at=now_iso(),
    )
