"""上下文组装领域模型：来源标识、上下文单元、组装结果与溯源清单。"""

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class SourceRef:
    """单元来源：唯一 ID + 时间戳 + 所属会话节点，支撑回答反向溯源。"""

    source_type: str
    source_id: str
    session_id: str
    timestamp: str
    message_id: int | None = None
    chunk_id: str | None = None
    doc_id: str | None = None
    doc_name: str | None = None
    section: str | None = None
    page: int | None = None
    table_no: str | None = None
    folder: str = ""
    content_hash: str = ""

    def to_dict(self) -> dict:
        return {
            "sourceType": self.source_type,
            "sourceId": self.source_id,
            "sessionId": self.session_id,
            "timestamp": self.timestamp,
            "messageId": self.message_id,
            "chunkId": self.chunk_id,
            "docId": self.doc_id,
            "docName": self.doc_name,
            "section": self.section,
            "page": self.page,
            "tableNo": self.table_no,
            "folder": self.folder,
            "contentHash": self.content_hash,
        }


@dataclass
class ContextUnit:
    unit_id: str
    layer: str
    text: str
    tokens: int
    weight: float
    ref: SourceRef
    flags: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


@dataclass
class DroppedUnit:
    unit_id: str
    layer: str
    reason: str
    tokens: int = 0
    excerpt: str = ""

    def to_dict(self) -> dict:
        return {
            "unitId": self.unit_id,
            "layer": self.layer,
            "reason": self.reason,
            "tokens": self.tokens,
            "excerpt": self.excerpt,
        }


@dataclass
class PersonaFact:
    key: str
    value: str
    confidence: float = 0.6
    source: str = "inferred"
    session_id: str = ""
    updated_at: str = ""


@dataclass
class TaskState:
    session_id: str
    version: int = 1
    status: str = "idle"
    topic: str = ""
    entities: list[str] = field(default_factory=list)
    scope_folders: list[str] = field(default_factory=list)
    last_intent: str = ""
    pending: list[str] = field(default_factory=list)
    turn: int = 0
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "status": self.status,
            "topic": self.topic,
            "entities": self.entities,
            "scopeFolders": self.scope_folders,
            "lastIntent": self.last_intent,
            "pending": self.pending,
            "turn": self.turn,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, session_id: str, data: dict | None) -> "TaskState":
        data = data or {}
        return cls(
            session_id=session_id,
            version=int(data.get("version", 1)),
            status=str(data.get("status", "idle")),
            topic=str(data.get("topic", "")),
            entities=[str(x) for x in data.get("entities", [])][:12],
            scope_folders=[str(x) for x in data.get("scopeFolders", [])][:8],
            last_intent=str(data.get("lastIntent", "")),
            pending=[str(x) for x in data.get("pending", [])][:6],
            turn=int(data.get("turn", 0)),
            updated_at=str(data.get("updatedAt", "")),
        )


@dataclass
class TurnSignal:
    question: str
    intent: str = "general"
    entities: list[str] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)


@dataclass
class AssemblyRequest:
    session_id: str
    question: str
    intent: str
    hits: list
    history: list[dict]
    mode: str = "kb"
    folders: list[str] = field(default_factory=list)
    model: str | None = None
    entities: list[str] = field(default_factory=list)
    task_state: TaskState | None = None
    persona: list[PersonaFact] = field(default_factory=list)
    template_id: str = "kb-answer"
    template_version: str = "1"


@dataclass
class ContextManifest:
    trace_id: str
    session_id: str
    created_at: str
    template_id: str
    template_version: str
    model: str | None
    budget: int
    used: int
    citation_map: dict[str, str]
    units: list[dict]
    integrity: bool = True

    def to_dict(self) -> dict:
        return {
            "traceId": self.trace_id,
            "sessionId": self.session_id,
            "createdAt": self.created_at,
            "templateId": self.template_id,
            "templateVersion": self.template_version,
            "model": self.model,
            "budget": self.budget,
            "used": self.used,
            "citationMap": self.citation_map,
            "units": self.units,
            "integrity": self.integrity,
        }


@dataclass
class AssemblyStats:
    trace_id: str
    budget: int
    effective: int
    fixed: int
    used: int
    utilization: float
    layer_tokens: dict
    layer_units: dict
    dropped: int
    duplicate: int
    noisy: int
    elapsed_ms: float
    mode: str = "on"

    def to_dict(self) -> dict:
        return {
            "traceId": self.trace_id,
            "budget": self.budget,
            "effective": self.effective,
            "fixed": self.fixed,
            "used": self.used,
            "utilization": round(self.utilization, 4),
            "layers": self.layer_units,
            "layerTokens": self.layer_tokens,
            "dropped": self.dropped,
            "duplicate": self.duplicate,
            "noisy": self.noisy,
            "elapsedMs": round(self.elapsed_ms, 2),
            "mode": self.mode,
        }


@dataclass
class ContextBundle:
    messages: list[dict]
    units: list[ContextUnit]
    evidence: list[ContextUnit]
    dropped: list[DroppedUnit]
    manifest: ContextManifest
    stats: AssemblyStats
    task_state: TaskState | None = None
    warnings: list[str] = field(default_factory=list)
    template_id: str = "kb-answer"
    template_version: str = "1"

    @property
    def evidence_hits(self) -> list:
        return [u.meta.get("hit") for u in self.evidence if u.meta.get("hit") is not None]

    def state_dict(self) -> dict[str, Any]:
        return {
            "stats": self.stats.to_dict(),
            "manifest": self.manifest.to_dict(),
            "warnings": self.warnings,
            "template": f"{self.template_id}@{self.template_version}",
        }
