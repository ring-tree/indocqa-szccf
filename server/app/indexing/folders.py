"""知识库目录：多级路径校验、层级推导、存量文档分类迁移。

目录以 "/" 分隔（如 `设备手册/螺杆空压机`），文档在 Chroma 元数据与
manifest 中同时记录 `folder`，检索按目录子树过滤。空目录由 manifest 的
`folders` 列表保存，保证"先建目录、后传文档"的可用性。
"""

import re

from app.core.exceptions import ParamError
from app.indexing.manifest import Manifest

DEFAULT_FOLDER = "未分类"
MAX_DEPTH = 6
MAX_SEGMENT = 40
MAX_PATH = 200

_BAD_SEGMENT = re.compile(r"[\x00-\x1f<>:\"|?*]")
_SPACE = re.compile(r"\s+")


class FolderPathError(ValueError):
    """目录路径不合法。"""


def normalize_folder(value: str | None, *, default: str | None = DEFAULT_FOLDER) -> str:
    """规范化目录路径；空值按 default 处理（default=None 表示根/全部）。"""
    if value is None or not str(value).strip():
        if default is None:
            return ""
        return default
    text = str(value).strip().replace("\\", "/")
    parts: list[str] = []
    for raw in text.split("/"):
        segment = _SPACE.sub(" ", raw).strip()
        if not segment or segment == ".":
            continue
        if segment == "..":
            raise FolderPathError("目录路径不能包含 ..")
        if len(segment) > MAX_SEGMENT:
            raise FolderPathError(f"单级目录名不能超过 {MAX_SEGMENT} 字")
        if _BAD_SEGMENT.search(segment):
            raise FolderPathError("目录名包含非法字符")
        parts.append(segment)
    if not parts:
        if default is None:
            return ""
        return default
    if len(parts) > MAX_DEPTH:
        raise FolderPathError(f"目录层级不能超过 {MAX_DEPTH} 级")
    path = "/".join(parts)
    if len(path) > MAX_PATH:
        raise FolderPathError(f"目录路径不能超过 {MAX_PATH} 字")
    return path


def parent_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def ancestors(path: str) -> list[str]:
    """含自身的全部祖先路径（从根到自身）。"""
    parts = [p for p in path.split("/") if p]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def is_within(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder + "/")


def descendants(folder: str, folders: list[str]) -> list[str]:
    """目录子树（含自身）；目录不存在时仍返回自身，便于空目录查询返回空结果。"""
    folder = normalize_folder(folder, default=None)
    if not folder:
        return []
    return sorted({folder, *(f for f in folders if is_within(f, folder))})


def folder_has_children(path: str, folders: list[str]) -> bool:
    return any(item.startswith(path + "/") for item in folders)


def leaf_folders(folders: list[str]) -> list[str]:
    """可存放文档的目录：没有子目录的末级目录。"""
    return [path for path in folders if not folder_has_children(path, folders)]


def _docs_by_folder(manifest: Manifest) -> set[str]:
    return {
        info.get("folder") or DEFAULT_FOLDER
        for info in manifest.documents().values()
        if info.get("folder")
    }


def validate_document_target(settings, path: str, *, allow_new: bool = True) -> str:
    """文档目标目录规范：只能落在末级目录；新建目录时祖先不得已有文档。"""
    target = normalize_folder(path, default=None)
    if not target:
        raise ParamError("目标目录不能为空")
    manifest = Manifest(Manifest.path_for(settings))
    folders = manifest.folders()
    doc_folders = _docs_by_folder(manifest)
    if target in folders:
        if folder_has_children(target, folders):
            raise ParamError(f"「{target}」包含子目录，只能选择最末级目录存放文档")
        return target
    if not allow_new:
        raise ParamError(f"目录不存在: {target}")
    for ancestor in ancestors(target)[:-1]:
        if ancestor in folders and ancestor in doc_folders:
            raise ParamError(f"「{ancestor}」下已有文档，不能再创建子目录")
    return target


def validate_folder_create(settings, path: str) -> str:
    """新建目录规范：已有文档的目录不能再创建子目录。"""
    target = normalize_folder(path, default=None)
    if not target:
        raise ParamError("目录路径不能为空")
    manifest = Manifest(Manifest.path_for(settings))
    folders = manifest.folders()
    if target in folders:
        return target
    doc_folders = _docs_by_folder(manifest)
    for ancestor in ancestors(target)[:-1]:
        if ancestor in folders and ancestor in doc_folders:
            raise ParamError(f"「{ancestor}」下已有文档，不能再创建子目录")
    return target


def scope_paths(settings, folder: str | None = None, folders: list[str] | None = None) -> list[str]:
    """合并单目录/多目录检索范围，展开为完整子树路径集合。"""
    requested: list[str] = []
    if folder:
        requested.append(folder)
    requested.extend(folders or [])
    if not requested:
        return []
    known = Manifest(Manifest.path_for(settings)).folders()
    result: set[str] = set()
    for raw in requested:
        try:
            path = normalize_folder(raw, default=None)
        except FolderPathError:
            continue
        if not path:
            continue
        result.add(path)
        result.update(item for item in known if is_within(item, path))
    return sorted(result)


def folder_counts(documents: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for info in documents.values():
        path = info.get("folder") or DEFAULT_FOLDER
        for item in ancestors(path):
            counts[item] = counts.get(item, 0) + 1
    return counts


def infer_folder(filename: str) -> str:
    """存量语料的一次性分类规则；未命中归档到“未分类”，可随时移动。"""
    name = (filename or "").lower()
    if "sourcebook" in name or "compressed_air" in name:
        return "行业标准/压缩空气系统"
    if "螺杆" in name or "screw" in name:
        return "设备手册/螺杆空压机"
    if "无油" in name or "静音" in name:
        return "设备手册/无油空压机"
    if "fixture" in name or "术语混写" in name:
        return "评测夹具"
    return DEFAULT_FOLDER


def normalize_display_names(settings) -> int:
    """修正历史上传文档的展示名：stored_name 带 8 位随机前缀时去掉前缀。"""
    import re

    from app.indexing import store

    manifest = Manifest(Manifest.path_for(settings))
    changed = 0
    for doc_id, info in manifest.documents().items():
        name = info.get("filename") or ""
        stored = info.get("stored_name") or ""
        match = re.match(r"^[0-9a-f]{8}_(.+)$", name)
        if not match or stored != name:
            continue
        new_name = match.group(1)
        store.update_doc_name(settings, doc_id, new_name)
        info["filename"] = new_name
        changed += 1
    if changed:
        manifest.save()
    return changed


def migrate_folders(settings) -> dict:
    """给缺少 folder 元数据的存量文档补分类，并补齐目录注册表（幂等）。

    只更新元数据，不重新解析、不重新嵌入；后端不一致或集合为空时跳过，
    避免把 manifest 改成与向量库不匹配的状态。
    """
    from app.indexing import store
    from app.indexing.embedder import get_embedder

    manifest = Manifest(Manifest.path_for(settings))
    documents = manifest.documents()
    if not documents:
        return {"migrated": 0, "documents": 0, "folders": 0, "skipped": "empty"}
    recorded = manifest.data.get("backend")
    current = get_embedder(settings).backend_id
    if recorded and recorded != current:
        return {
            "migrated": 0,
            "documents": len(documents),
            "folders": 0,
            "skipped": "backend_mismatch",
        }
    if store.is_empty(settings):
        return {"migrated": 0, "documents": len(documents), "folders": 0, "skipped": "no_vectors"}
    migrated = 0
    for doc_id, info in documents.items():
        if info.get("folder"):
            continue
        folder = infer_folder(info.get("filename", ""))
        store.update_doc_folder(settings, doc_id, folder)
        manifest.set_doc_folder(doc_id, folder)
        migrated += 1
    for info in documents.values():
        for path in ancestors(info.get("folder") or DEFAULT_FOLDER):
            manifest.ensure_folder(path)
    if migrated:
        manifest.save()
    return {
        "migrated": migrated,
        "documents": len(documents),
        "folders": len(manifest.folders()),
    }
