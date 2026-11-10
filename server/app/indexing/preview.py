"""语料入库预检：解析演练 + 质量信号，不写入任何存储。

判定三档：ok（可直接入库）/ warning（可入库但需知悉）/ rejected（不建议入库，先处理原始文档）。
"""

import pathlib
import re

from app.core.config import get_settings
from app.parsing.chunker import build_chunks
from app.parsing.loader import load_document

_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")
_TOC_LINE = re.compile(r"\.{4,}\s*\d+\s*$")


def preview_document(path, settings=None, classify: bool = False) -> dict:
    settings = settings or get_settings()
    path = pathlib.Path(path)
    doc = load_document(path)
    children, _parents = build_chunks(doc)

    total_chars = sum(len(p.text) for p in doc.paragraphs)
    pages = doc.pages or 1
    chars_per_page = total_chars / max(pages, 1)
    cjk = sum(len(_CJK.findall(p.text)) for p in doc.paragraphs)
    latin = sum(len(_LATIN.findall(p.text)) for p in doc.paragraphs)
    toc_like = sum(
        1 for p in doc.paragraphs if sum(1 for l in p.text.splitlines() if _TOC_LINE.search(l)) >= 2
    )
    toc_ratio = toc_like / max(len(doc.paragraphs), 1)

    from app.indexing.manifest import Manifest

    content_hash = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    existed = Manifest(pathlib.Path(settings.chroma_dir) / "manifest.json").has_hash(content_hash)

    issues: list[dict] = []
    status = "ok"
    if pages >= 3 and chars_per_page < 50:
        status = "rejected"
        issues.append(
            {
                "level": "error",
                "message": f"平均每页仅 {chars_per_page:.0f} 字符，疑似扫描件（无文本层）",
                "hint": "换用带文本层的 PDF，或先做 OCR 处理",
            }
        )
    if toc_ratio > 0.4:
        status = "warning" if status == "ok" else status
        issues.append(
            {
                "level": "warning",
                "message": f"目录样式段落占 {toc_ratio:.0%}，目录页会作为噪声参与检索",
                "hint": "可接受；解析层已做目录块过滤",
            }
        )
    if not children:
        status = "rejected"
        issues.append(
            {
                "level": "error",
                "message": "无可入库内容（解析后子块为 0）",
                "hint": "检查文档内容与格式",
            }
        )
    elif len(children) < 3 and pages and pages > 10:
        status = "warning" if status == "ok" else status
        issues.append(
            {
                "level": "warning",
                "message": f"{pages} 页仅产出 {len(children)} 个子块，有效内容偏少",
                "hint": "确认正文是否为图片或被过滤",
            }
        )
    if existed:
        issues.append(
            {"level": "info", "message": "内容已存在于当前知识库，重复入库将自动跳过", "hint": ""}
        )

    language = "zh" if cjk >= latin else "en"
    report = {
        "filename": path.name,
        "status": status,
        "pages": doc.pages,
        "paragraphs": len(doc.paragraphs),
        "tables": len(doc.tables),
        "estimatedChunks": len(children),
        "totalChars": total_chars,
        "charsPerPage": round(chars_per_page),
        "language": language,
        "tocRatio": round(toc_ratio, 2),
        "duplicated": bool(existed),
        "issues": issues,
    }
    if classify:
        report["classification"] = _classify_preview(settings, doc)
    return report


def _classify_preview(settings, doc) -> dict:
    """预检后可选执行 AI 分类；未配置模型时返回不可用标记而非报错。"""
    import asyncio

    from app.classification import service as classify_service

    if not classify_service.is_available(settings):
        return {"available": False, "reason": "no_model"}
    text = "\n".join(p.text for p in doc.paragraphs)[:2000]
    try:
        return asyncio.run(
            classify_service.classify_text(settings, doc.filename, text, use_llm=False)
        )
    except Exception as e:  # noqa: BLE001 分类失败不应影响预检结果
        return {
            "available": True,
            "candidates": [],
            "reason": "classify_failed",
            "error": str(e),
        }


def main(argv: list[str]) -> int:
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    if not argv:
        print("用法: python -m app.indexing.preview <文件或目录> ...")
        return 1
    bad = 0
    for target in argv:
        p = pathlib.Path(target)
        files = sorted(p.iterdir()) if p.is_dir() else [p]
        for f in files:
            if not f.suffix.lower() in {".pdf", ".md", ".txt", ".docx"} or f.name == "sources.md":
                continue
            r = preview_document(f)
            flag = {"ok": "通过", "warning": "警示", "rejected": "拒收"}[r["status"]]
            print(
                f"[{flag}] {r['filename']}: {r['pages']}页/{r['estimatedChunks']}块/{r['tables']}表 "
                f"语言={r['language']} 去重={r['duplicated']}"
            )
            for i in r["issues"]:
                print(
                    f"    - [{i['level']}] {i['message']}"
                    + (f"（{i['hint']}）" if i["hint"] else "")
                )
            bad += r["status"] == "rejected"
    return 1 if bad else 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
