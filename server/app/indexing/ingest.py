"""文档入库：解析 → 切块 → 嵌入 → 入库，内容哈希幂等。

用法：python -m app.indexing.ingest [目录或文件]（缺省取配置 corpus_dir）
"""

import pathlib
import sys
import time

from app.core.config import get_settings
from app.indexing import store
from app.indexing.folders import ancestors, normalize_folder, validate_document_target
from app.indexing.lock import write_guard
from app.indexing.manifest import Manifest
from app.parsing.chunker import build_chunks
from app.parsing.loader import MD_EXTS, load_document

SKIP_FILES = {"sources.md"}
ALLOWED_EXTS = {".pdf"} | MD_EXTS


def ingest_file(
    path: pathlib.Path,
    settings=None,
    stored_name: str | None = None,
    folder: str | None = None,
    progress=None,
    display_name: str | None = None,
) -> dict:
    settings = settings or get_settings()
    started = time.perf_counter()
    content_hash = _hash_of(path)
    target_folder = validate_document_target(settings, normalize_folder(folder))
    manifest = Manifest(Manifest.path_for(settings))
    with write_guard(settings):
        # 后端一致性先于去重：换后端后旧 manifest 不应让入库被“内容已存在”静默跳过
        _ensure_backend_match(settings, manifest)
        existed = manifest.has_hash(content_hash)
        if existed:
            info = manifest.get(existed)
            if progress:
                progress("done", 0, 0)
            return {
                "docId": existed,
                "filename": info["filename"],
                "pages": info.get("pages"),
                "chunks": info.get("chunks", 0),
                "folder": info.get("folder") or target_folder,
                "durationMs": int((time.perf_counter() - started) * 1000),
                "duplicated": True,
            }

        doc = load_document(path)
        if display_name:
            # 上传临时文件带随机前缀，展示与引用统一使用用户原始文件名
            doc.filename = display_name
        if progress:
            progress("parsing", 0, 0)
        children, parents = build_chunks(doc)
        if progress:
            progress("chunking", len(children), len(children))
        if not children:
            if progress:
                progress("done", 0, 0)
            return {
                "docId": doc.doc_id,
                "filename": doc.filename,
                "pages": doc.pages,
                "chunks": 0,
                "folder": target_folder,
                "durationMs": 0,
                "duplicated": False,
            }
        try:
            store.add_children(
                settings, children, parents, doc.filename, target_folder, progress=progress
            )
        except Exception:
            # 补偿：向量/父块/BM25 任一环节失败时不留下孤儿块
            try:
                store.delete_doc(settings, doc.doc_id)
            except Exception:  # noqa: BLE001, S110 回滚失败不应掩盖原始异常
                pass
            raise
        manifest.upsert(
            doc.doc_id,
            {
                "filename": doc.filename,
                "hash": content_hash,
                "pages": doc.pages,
                "chunks": len(children),
                "tables": len(doc.tables),
                "stored_name": stored_name,
                "backend": store.get_embedder(settings).backend_id,
                "folder": target_folder,
            },
        )
        for path_item in ancestors(target_folder):
            manifest.ensure_folder(path_item)
        manifest.save()
        if progress:
            progress("done", len(children), len(children))
        return {
            "docId": doc.doc_id,
            "filename": doc.filename,
            "pages": doc.pages,
            "chunks": len(children),
            "folder": target_folder,
            "durationMs": int((time.perf_counter() - started) * 1000),
            "duplicated": False,
        }


def ingest_dir(directory: pathlib.Path, settings=None, folder: str | None = None) -> list[dict]:
    settings = settings or get_settings()
    results = []
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix.lower() in ALLOWED_EXTS and path.name not in SKIP_FILES:
            results.append(ingest_file(path, settings, folder=folder))
    return results


def _ensure_backend_match(settings, manifest: Manifest) -> None:
    """向量库集合按嵌入后端隔离，但 manifest 是全局的——切换后端后旧 manifest
    会让入库被“内容已存在”跳过，得到静默空库。此处强制校验并给出重建指引。"""
    from app.core.exceptions import BackendMismatchError
    from app.indexing.embedder import get_embedder

    recorded = manifest.data.get("backend")
    current = get_embedder(settings).backend_id
    if recorded and recorded != current:
        raise BackendMismatchError(
            f"知识库由嵌入后端 {recorded} 构建，当前后端为 {current}",
            detail=f"请执行 python -m app.indexing.ingest --rebuild 重建，或删除 {settings.chroma_dir}",
        )
    if not recorded:
        manifest.data["backend"] = current


def _hash_of(path: pathlib.Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    settings = get_settings()
    args = list(argv)
    if "--preview" in args:
        from app.indexing.preview import main as preview_main

        return preview_main([a for a in args if a != "--preview"])
    if "--rebuild" in args:
        import shutil

        with write_guard(settings):
            shutil.rmtree(pathlib.Path(settings.chroma_dir), ignore_errors=True)
        print(f"已清空向量库目录 {settings.chroma_dir}，开始全量重建")
        args = [a for a in args if a != "--rebuild"]
    target = pathlib.Path(args[0]) if args else pathlib.Path(settings.corpus_dir)
    targets = (
        [target]
        if target.is_file()
        else sorted(
            p
            for p in target.iterdir()
            if p.is_file() and p.suffix.lower() in ALLOWED_EXTS and p.name not in SKIP_FILES
        )
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
