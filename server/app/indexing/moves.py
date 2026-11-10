"""批量移动与撤销：文档目录变更的操作日志与反向恢复。

操作日志保存在 chroma 目录的 move_ops.json（最多 50 条，撤销有效期 10 分钟）。
撤销只恢复"当前仍在目标目录"的文档，避免覆盖后续的人工移动。
"""

import json
import pathlib
import time
from uuid import uuid4

from app.core.config import get_settings
from app.core.exceptions import ParamError
from app.indexing import store
from app.indexing.folders import (
    DEFAULT_FOLDER,
    FolderPathError,
    ancestors,
    normalize_folder,
    validate_document_target,
)
from app.indexing.lock import write_guard
from app.indexing.manifest import Manifest

UNDO_TTL_SECONDS = 600
MAX_OPS = 50


def _ops_path(settings) -> pathlib.Path:
    return pathlib.Path(settings.chroma_dir) / "move_ops.json"


def _load_ops(settings) -> dict:
    path = _ops_path(settings)
    if not path.exists():
        return {"operations": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"operations": []}
    data.setdefault("operations", [])
    return data


def _save_ops(settings, data: dict) -> None:
    data["operations"] = data["operations"][-MAX_OPS:]
    path = _ops_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def batch_move(settings, doc_ids: list[str], folder: str) -> dict:
    """把多篇文档移动到同一目录；返回 operationId 供撤销。"""
    settings = settings or get_settings()
    try:
        target = normalize_folder(folder)
    except FolderPathError as e:
        raise ParamError(str(e)) from e
    target = validate_document_target(settings, target)
    if not doc_ids:
        raise ParamError("至少选择一篇文档")

    operation_id = f"mv_{int(time.time())}_{uuid4().hex[:8]}"
    moved: list[str] = []
    skipped: list[str] = []
    skipped_details: list[dict] = []
    failed: list[dict] = []
    items: list[dict] = []

    with write_guard(settings):
        manifest = Manifest(Manifest.path_for(settings))
        existing = {doc_id: manifest.get(doc_id) for doc_id in doc_ids}
        for doc_id in doc_ids:
            info = existing.get(doc_id)
            if not info:
                failed.append({"docId": doc_id, "reason": "文档不存在"})
                continue
            current = info.get("folder") or DEFAULT_FOLDER
            if current == target:
                skipped.append(doc_id)
                skipped_details.append({"docId": doc_id, "reason": "已在目标目录"})
                continue
            try:
                store.update_doc_folder(settings, doc_id, target)
            except Exception as e:  # noqa: BLE001 单篇失败不影响其余文档
                failed.append({"docId": doc_id, "reason": str(e)})
                continue
            manifest.set_doc_folder(doc_id, target)
            items.append({"docId": doc_id, "from": current})
            moved.append(doc_id)
        for path in ancestors(target):
            manifest.ensure_folder(path)
        manifest.save()

        if items:
            data = _load_ops(settings)
            data["operations"].append(
                {
                    "id": operation_id,
                    "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "timestamp": time.time(),
                    "target": target,
                    "items": items,
                    "undone": False,
                }
            )
            _save_ops(settings, data)

    return {
        "operationId": operation_id if items else None,
        "target": target,
        "moved": moved,
        "skipped": skipped,
        "skippedDetails": skipped_details,
        "failed": failed,
    }


def undo_move(settings, operation_id: str) -> dict:
    """撤销一次批量移动；超过 10 分钟或已被撤销时拒绝。"""
    settings = settings or get_settings()
    with write_guard(settings):
        data = _load_ops(settings)
        operation = next((op for op in data["operations"] if op.get("id") == operation_id), None)
        if not operation:
            raise ParamError("操作记录不存在或已过期")
        if operation.get("undone"):
            raise ParamError("该操作已撤销")
        if time.time() - float(operation.get("timestamp", 0)) > UNDO_TTL_SECONDS:
            raise ParamError("撤销已过期（超过 10 分钟）")

        manifest = Manifest(Manifest.path_for(settings))
        target = operation.get("target") or DEFAULT_FOLDER
        restored: list[str] = []
        failed: list[dict] = []
        for item in operation.get("items", []):
            doc_id = item.get("docId", "")
            info = manifest.get(doc_id)
            if not info:
                failed.append({"docId": doc_id, "reason": "文档不存在"})
                continue
            if (info.get("folder") or DEFAULT_FOLDER) != target:
                failed.append({"docId": doc_id, "reason": "文档已被再次移动"})
                continue
            source = item.get("from") or DEFAULT_FOLDER
            store.update_doc_folder(settings, doc_id, source)
            manifest.set_doc_folder(doc_id, source)
            for path in ancestors(source):
                manifest.ensure_folder(path)
            restored.append(doc_id)
        manifest.save()
        operation["undone"] = True
        _save_ops(settings, data)
        return {"operationId": operation_id, "restored": restored, "failed": failed}
