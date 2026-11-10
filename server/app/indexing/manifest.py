"""入库清单：chroma 目录下的 manifest.json，记录文档元信息与目录注册表。

manifest 同时是健康检查、幂等去重与目录管理的唯一事实源；目录信息与
Chroma 元数据双写，检索侧只依赖 Chroma，管理侧只依赖 manifest。
"""

import json
import pathlib
import time


class Manifest:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.data: dict = {"documents": {}}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
        self.data.setdefault("documents", {})
        self.data.setdefault("folders", [])

    @classmethod
    def path_for(cls, settings) -> pathlib.Path:
        return pathlib.Path(settings.chroma_dir) / "manifest.json"

    def get(self, doc_id: str) -> dict | None:
        return self.data["documents"].get(doc_id)

    def has_hash(self, content_hash: str) -> str | None:
        for doc_id, info in self.data["documents"].items():
            if info.get("hash") == content_hash:
                return doc_id
        return None

    def upsert(self, doc_id: str, info: dict) -> None:
        info.setdefault("ingestedAt", time.strftime("%Y-%m-%dT%H:%M:%S"))
        self.data["documents"][doc_id] = info

    def remove(self, doc_id: str) -> None:
        self.data["documents"].pop(doc_id, None)

    def documents(self) -> dict:
        return self.data["documents"]

    def stats(self) -> tuple[int, int]:
        docs = self.data["documents"]
        return len(docs), sum(d.get("chunks", 0) for d in docs.values())

    # ---- 目录注册表 ----
    def folders(self) -> list[str]:
        """显式目录 ∪ 文档所在目录（含祖先），排序后返回。"""
        from app.indexing.folders import ancestors

        known = set(self.data.get("folders", []))
        for info in self.data["documents"].values():
            folder = info.get("folder")
            if folder:
                known.update(ancestors(folder))
        return sorted(known)

    def ensure_folder(self, path: str) -> None:
        if path:
            self.data.setdefault("folders", [])
            if path not in self.data["folders"]:
                self.data["folders"].append(path)

    def remove_folder(self, path: str) -> None:
        self.data["folders"] = [f for f in self.data.get("folders", []) if f != path]

    def set_doc_folder(self, doc_id: str, folder: str) -> None:
        info = self.data["documents"].get(doc_id)
        if info is not None:
            info["folder"] = folder

    def docs_in_folder(self, folder: str) -> list[str]:
        from app.indexing.folders import is_within

        return [
            doc_id
            for doc_id, info in self.data["documents"].items()
            if is_within(info.get("folder") or "", folder)
        ]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
