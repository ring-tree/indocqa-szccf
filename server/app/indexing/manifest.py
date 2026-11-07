"""入库清单：chroma 目录下的 manifest.json，记录文档元信息（健康检查与幂等去重的唯一事实源）。"""

import json
import pathlib
import time


class Manifest:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.data: dict = {"documents": {}}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

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

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
