"""文档入库：解析 → 切块 → 嵌入 → 入库，内容哈希幂等。

用法：python -m app.indexing.ingest [目录或文件]（缺省取配置 corpus_dir）
"""

import pathlib
import sys
import time

from app.core.config import get_settings
from app.indexing.manifest import Manifest
from app.indexing import store
from app.parsing.chunker import build_chunks
from app.parsing.loader import MD_EXTS, load_document

SKIP_FILES = {"sources.md"}
ALLOWED_EXTS = {".pdf"} | MD_EXTS


def ingest_file(path: pathlib.Path, settings=None, stored_name: str | None = None) -> dict:
    settings = settings or get_settings()
    started = time.perf_counter()
    content_hash = _hash_of(path)
    manifest = Manifest(pathlib.Path(settings.chroma_dir) / "manifest.json")
    existed = manifest.has_hash(content_hash)
    if existed:
        info = manifest.get(existed)
        return {
            "docId": existed,
            "filename": info["filename"],
            "pages": info.get("pages"),
            "chunks": info.get("chunks", 0),
            "durationMs": int((time.perf_counter() - started) * 1000),
            "duplicated": True,
        }

    doc = load_document(path)
    children, parents = build_chunks(doc)
    if not children:
        return {"docId": doc.doc_id, "filename": doc.filename, "pages": doc.pages,
                "chunks": 0, "durationMs": 0, "duplicated": False}
    store.add_children(settings, children, parents, doc.filename)
    manifest.upsert(doc.doc_id, {
        "filename": doc.filename,
        "hash": content_hash,
        "pages": doc.pages,
        "chunks": len(children),
        "tables": len(doc.tables),
        "stored_name": stored_name,
    })
    manifest.save()
    return {
        "docId": doc.doc_id,
        "filename": doc.filename,
        "pages": doc.pages,
        "chunks": len(children),
        "durationMs": int((time.perf_counter() - started) * 1000),
        "duplicated": False,
    }


def ingest_dir(directory: pathlib.Path, settings=None) -> list[dict]:
    settings = settings or get_settings()
    results = []
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix.lower() in ALLOWED_EXTS and path.name not in SKIP_FILES:
            results.append(ingest_file(path, settings))
    return results


def _hash_of(path: pathlib.Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    settings = get_settings()
    target = pathlib.Path(argv[0]) if argv else pathlib.Path(settings.corpus_dir)
    targets = [target] if target.is_file() else sorted(
        p for p in target.iterdir()
        if p.is_file() and p.suffix.lower() in ALLOWED_EXTS and p.name not in SKIP_FILES
    )
    if not targets:
        print(f"目录无可用文档: {target}")
        return 1
    total = 0
    for path in targets:
        result = ingest_file(path, settings)
        flag = " [跳过，内容已存在]" if result["duplicated"] else ""
        print(f"{result['filename']}: {result['chunks']} chunks, {result['durationMs']}ms{flag}")
        total += result["chunks"]
    print(f"共 {len(targets)} 份文档，{total} 个子块")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
