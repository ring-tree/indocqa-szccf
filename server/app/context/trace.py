"""溯源：单元唯一 ID、引用映射、清单构建与回答反查审计。"""

import hashlib
import re
from dataclasses import dataclass

from app.context.schema import ContextManifest, ContextUnit

_CITED = re.compile(r"\[(\d+)\]")


@dataclass
class TraceAuditReport:
    total: int
    resolved: int
    missing: list[str]
    integrity: bool

    @property
    def rate(self) -> float:
        return self.resolved / self.total if self.total else 1.0


def build_unit_id(session_id: str, layer: str, source_id: str, text: str) -> str:
    digest = hashlib.sha1(f"{session_id}|{layer}|{source_id}|{text}".encode()).hexdigest()[:16]
    return f"ctx_{digest}"


def _ref_complete(unit: ContextUnit) -> bool:
    ref = unit.ref
    if not ref.source_id or not ref.session_id:
        return False
    if unit.layer == "evidence":
        return bool(ref.chunk_id and ref.doc_id and ref.doc_name and ref.section)
    if unit.layer == "history":
        return ref.message_id is not None
    return True


def build_manifest(
    trace_id: str,
    session_id: str,
    created_at: str,
    template_id: str,
    template_version: str,
    model: str | None,
    budget: int,
    used: int,
    units: list[ContextUnit],
    evidence: list[ContextUnit],
) -> ContextManifest:
    entries = []
    integrity = True
    for unit in units:
        if not _ref_complete(unit):
            integrity = False
        entries.append(
            {
                "unitId": unit.unit_id,
                "layer": unit.layer,
                "weight": round(unit.weight, 4),
                "tokens": unit.tokens,
                "flags": unit.flags,
                "excerpt": unit.text[:80],
                "ref": unit.ref.to_dict(),
            }
        )
    citation_map = {str(index): unit.unit_id for index, unit in enumerate(evidence, start=1)}
    if len(set(citation_map.values())) != len(citation_map):
        integrity = False
    return ContextManifest(
        trace_id=trace_id,
        session_id=session_id,
        created_at=created_at,
        template_id=template_id,
        template_version=template_version,
        model=model,
        budget=budget,
        used=used,
        citation_map=citation_map,
        units=entries,
        integrity=integrity,
    )


def audit_trace(answer: str, manifest: ContextManifest) -> TraceAuditReport:
    numbers = []
    for raw in _CITED.findall(answer or ""):
        number = int(raw)
        if number not in numbers:
            numbers.append(number)
    missing = [str(n) for n in numbers if str(n) not in manifest.citation_map]
    units = {entry["unitId"]: entry for entry in manifest.units}
    for number in numbers:
        unit_id = manifest.citation_map.get(str(number))
        if not unit_id or unit_id not in units:
            continue
        ref = units[unit_id]["ref"]
        if not ref.get("docId") or not ref.get("docName"):
            missing.append(str(number))
    missing = list(dict.fromkeys(missing))
    return TraceAuditReport(
        total=len(numbers),
        resolved=len(numbers) - len(missing),
        missing=missing,
        integrity=manifest.integrity and not missing,
    )
