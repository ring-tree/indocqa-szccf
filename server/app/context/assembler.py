"""上下文组装编排器：采集 → 噪声过滤 → 去重 → 权重 → 预算 → 融合 → 模板 → 溯源。"""

import time

from app.context import dedup as dedup_mod
from app.context import noise as noise_mod
from app.context import templates, trace, weight
from app.context.budget import plan_budget
from app.context.errors import SessionIsolationError, TemplateValidationError
from app.context.schema import (
    AssemblyRequest,
    AssemblyStats,
    ContextBundle,
    ContextUnit,
    DroppedUnit,
    SourceRef,
    TurnSignal,
    content_hash,
    new_trace_id,
    now_iso,
)
from app.context.task_state import update_task_state
from app.context.tokens import get_counter

_PERSONA_LABEL = {
    "language": "语言",
    "style": "回答风格",
    "role": "岗位",
    "focus": "关注领域",
    "preference": "偏好",
}


class ContextAssembler:
    def __init__(self, settings, embedder=None):
        self.settings = settings
        self.embedder = embedder

    def assemble(self, req: AssemblyRequest) -> ContextBundle:
        started = time.perf_counter()
        counter = get_counter(self.settings)
        warnings: list[str] = []
        units = self._collect(req, counter)
        kept, noise_dropped = noise_mod.filter_noise(units, self.settings)
        vectors, vector_warning = self._vectors(req)
        if vector_warning:
            warnings.append(vector_warning)
        kept, duplicate_dropped = dedup_mod.deduplicate(kept, vectors, self.settings, self.embedder)
        kept = weight.score_units(
            kept,
            self.settings,
            intent=req.intent,
            entities=weight.extract_entities(req.question, req.entities),
            persona=req.persona,
            task=req.task_state,
        )
        spec = templates.get_template(req.template_id, req.template_version)
        fixed = counter.count("\n".join(rule.text for rule in spec.active_rules(req.intent)))
        plan = plan_budget(
            kept,
            self.settings,
            counter,
            fixed_tokens=fixed,
            question_tokens=counter.count_message("user", req.question),
        )

        evidence = sorted(
            [unit for unit in plan.kept if unit.layer == "evidence"],
            key=lambda u: u.weight,
            reverse=True,
        )
        persona = sorted(
            [unit for unit in plan.kept if unit.layer == "persona"],
            key=lambda u: u.weight,
            reverse=True,
        )
        task = [unit for unit in plan.kept if unit.layer == "task"]
        history = sorted(
            [unit for unit in plan.kept if unit.layer == "history"],
            key=lambda u: u.meta.get("message_id") or 0,
        )

        messages = templates.render(
            spec, req.intent, req.question, evidence, persona, task, history
        )
        errors = templates.validate(
            messages, spec, req.intent, req.question, evidence, counter, plan.effective
        )
        template_used = spec
        if errors:
            fallback = templates.get_template("kb-answer", "0")
            messages = templates.render(
                fallback, req.intent, req.question, evidence, persona, task, history
            )
            fallback_errors = templates.validate(
                messages, fallback, req.intent, req.question, evidence, counter, plan.effective
            )
            if fallback_errors:
                raise TemplateValidationError("模板渲染校验失败", detail=",".join(fallback_errors))
            errors = fallback_errors
            template_used = fallback
            warnings.append("template_fallback")

        used = sum(counter.count_message(m["role"], m["content"]) for m in messages)
        manifest = trace.build_manifest(
            trace_id=new_trace_id(),
            session_id=req.session_id,
            created_at=now_iso(),
            template_id=template_used.template_id,
            template_version=template_used.version,
            model=req.model,
            budget=plan.effective,
            used=used,
            units=[*persona, *task, *history, *evidence],
            evidence=evidence,
        )
        if not manifest.integrity:
            warnings.append("trace_integrity")
        state = update_task_state(
            req.task_state,
            TurnSignal(
                question=req.question,
                intent=req.intent,
                entities=weight.extract_entities(req.question, req.entities),
                folders=req.folders,
            ),
            req.session_id,
        )
        dropped: list[DroppedUnit] = [*noise_dropped, *duplicate_dropped, *plan.dropped]
        elapsed = (time.perf_counter() - started) * 1000

        stats = AssemblyStats(
            trace_id=manifest.trace_id,
            budget=plan.effective,
            effective=plan.effective,
            fixed=plan.fixed,
            used=used,
            utilization=used / plan.effective if plan.effective else 0.0,
            layer_tokens=plan.layer_tokens,
            layer_units=plan.layer_units,
            dropped=len(dropped),
            duplicate=sum(1 for item in dropped if item.reason.startswith("duplicate")),
            noisy=sum(1 for item in dropped if item.reason.startswith("noise")),
            elapsed_ms=elapsed,
            mode=str(getattr(self.settings, "context_mode", "on")),
        )
        return ContextBundle(
            messages=messages,
            units=[*persona, *task, *history, *evidence],
            evidence=evidence,
            dropped=dropped,
            manifest=manifest,
            stats=stats,
            task_state=state,
            warnings=warnings,
            template_id=template_used.template_id,
            template_version=template_used.version,
        )

    def _collect(self, req: AssemblyRequest, counter) -> list[ContextUnit]:
        timestamp = now_iso()
        units: list[ContextUnit] = []
        max_rrf = max((float(getattr(hit, "rrf", 0.0) or 0.0) for hit in req.hits), default=0.0)

        for hit in req.hits:
            sim = getattr(hit, "sim", None)
            rrf = float(getattr(hit, "rrf", 0.0) or 0.0)
            rel = float(sim) if sim is not None else (rrf / max_rrf if max_rrf else 0.0)
            ref = SourceRef(
                source_type="chunk",
                source_id=str(getattr(hit, "chunk_id", "")),
                session_id=req.session_id,
                timestamp=timestamp,
                chunk_id=str(getattr(hit, "chunk_id", "")),
                doc_id=str(getattr(hit, "doc_id", "")),
                doc_name=str(getattr(hit, "doc_name", "")),
                section=str(getattr(hit, "section", "")),
                page=getattr(hit, "page", None),
                table_no=getattr(hit, "table_no", None),
                folder=str(getattr(hit, "folder", "")),
                content_hash=content_hash(getattr(hit, "text", "")),
            )
            self._check_scope(ref, req.session_id, "evidence")
            location = f"{ref.doc_name or ''} · {ref.section or ''}"
            if ref.page:
                location += f" · 第{ref.page}页"
            if ref.table_no:
                location += f" · {ref.table_no}"
            header_tokens = counter.count(f"[1] {location}") + 4
            units.append(
                ContextUnit(
                    unit_id=trace.build_unit_id(
                        req.session_id, "evidence", ref.source_id, ref.content_hash
                    ),
                    layer="evidence",
                    text=str(getattr(hit, "text", "")),
                    tokens=counter.count_message("user", str(getattr(hit, "text", "")))
                    + header_tokens,
                    weight=0.0,
                    ref=ref,
                    meta={
                        "hit": hit,
                        "sim": sim,
                        "rrf": rrf,
                        "rel": rel,
                        "kind": str(getattr(hit, "kind", "text")),
                    },
                )
            )

        for index, row in enumerate(req.history):
            text = str(row.get("content", "") or "")
            if not text.strip():
                continue
            message_id = row.get("id")
            if message_id is None and str(row.get("messageId", "")).isdigit():
                message_id = int(row["messageId"])
            ref = SourceRef(
                source_type="history",
                source_id=str(message_id if message_id is not None else index),
                session_id=req.session_id,
                timestamp=str(row.get("createdAt") or timestamp),
                message_id=int(message_id) if message_id is not None else None,
                content_hash=content_hash(text),
            )
            self._check_scope(ref, req.session_id, "history")
            units.append(
                ContextUnit(
                    unit_id=trace.build_unit_id(
                        req.session_id, "history", ref.source_id, ref.content_hash
                    ),
                    layer="history",
                    text=text,
                    tokens=counter.count_message(str(row.get("role", "user")), text),
                    weight=0.0,
                    ref=ref,
                    meta={"role": row.get("role", "user"), "message_id": message_id},
                )
            )

        for fact in req.persona:
            text = f"{_PERSONA_LABEL.get(fact.key, fact.key)}：{fact.value}"
            ref = SourceRef(
                source_type="persona",
                source_id=fact.key,
                session_id=fact.session_id,
                timestamp=fact.updated_at or timestamp,
                content_hash=content_hash(text),
            )
            if fact.session_id:
                self._check_scope(ref, req.session_id, "persona")
            units.append(
                ContextUnit(
                    unit_id=trace.build_unit_id(req.session_id, "persona", fact.key, text),
                    layer="persona",
                    text=text,
                    tokens=counter.count_message("system", text),
                    weight=0.0,
                    ref=ref,
                    meta={"confidence": fact.confidence, "source": fact.source},
                )
            )

        state = req.task_state
        if state is not None:
            parts = []
            if state.topic:
                parts.append(f"主题：{state.topic}")
            if state.entities:
                parts.append(f"关键实体：{'、'.join(state.entities)}")
            if state.scope_folders:
                parts.append(f"检索范围：{'、'.join(state.scope_folders)}")
            if not parts:
                parts.append("主题：新会话")
            text = "；".join(parts)
            ref = SourceRef(
                source_type="task",
                source_id=f"v{state.version}",
                session_id=req.session_id,
                timestamp=state.updated_at or timestamp,
                content_hash=content_hash(text),
            )
            self._check_scope(ref, req.session_id, "task")
            units.append(
                ContextUnit(
                    unit_id=trace.build_unit_id(
                        req.session_id, "task", ref.source_id, ref.content_hash
                    ),
                    layer="task",
                    text=text,
                    tokens=counter.count_message("system", text),
                    weight=0.0,
                    ref=ref,
                    meta={"version": state.version},
                )
            )
        return units

    @staticmethod
    def _check_scope(ref: SourceRef, session_id: str, layer: str) -> None:
        if ref.session_id and ref.session_id != session_id:
            raise SessionIsolationError(
                "上下文单元跨会话", detail=f"{layer}:{ref.source_id}->{ref.session_id}"
            )

    def _vectors(self, req: AssemblyRequest) -> tuple[dict, str]:
        chunk_ids = [
            str(getattr(hit, "chunk_id", "")) for hit in req.hits if getattr(hit, "chunk_id", None)
        ]
        if not chunk_ids:
            return {}, ""
        try:
            from app.indexing import store

            return store.vectors_for(self.settings, chunk_ids), ""
        except (ImportError, OSError, RuntimeError, ValueError):
            # 向量库不可用时退化为词法去重，不阻塞问答
            return {}, "dedup_vectors_unavailable"
