"""清洁检测：叙事禁词（仓库独立规范）+ 代码/文档痕迹扫描。

用法：python scripts/clean_check.py [--fix 提示人工修复，无自动改写]
退出码非 0 表示存在高危项（禁词命中 / app 层 print / 假 TODO / emoji）。
"""

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEXT_EXT = {
    ".py",
    ".md",
    ".ts",
    ".tsx",
    ".js",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".css",
    ".html",
}
SKIP_DIRS = {
    "node_modules",
    "dist",
    ".git",
    "__pycache__",
    ".venv",
    "data",
    "chroma",
    "models",  # 第三方模型权重（tokenizer 词表天然含 emoji 字符），不属 authored 内容
}

# 仓库叙事独立规范禁词（赛务词/组织词/协同痕迹）
BANNED = re.compile(
    r"比赛|赛题|命题|参赛|赛道|方向[一二三]|场景 ?[0-9]|答辩|盲测|评分|分值|"
    r"腾讯|SZCCF|深职大|XBuddy|乐谱兰斯|项目企划|indocqa-(tencent|szccf)|宿主仓库|共享内核|兄弟仓库"
)
# 需要豁免的合法场景（如本脚本自身、规范文档所在的工作区不进仓库）
SELF_EXEMPT = re.compile(r"clean_check\.py")
EMOJI = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
PRINT_IN_APP = re.compile(r"^\s*print\(", re.MULTILINE)


def _cli_main_lines(text: str) -> set[int]:
    """包内 CLI 模块的 main() 行号集合：python -m 入口的 stdout 输出属预期行为，豁免。"""
    import ast

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    has_entry = "__main__" in text
    if not has_entry:
        return set()
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            for i in range(node.lineno, node.end_lineno + 1):
                lines.add(i)
    return lines


def iter_files():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_EXT:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        yield path


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.parse_args()
    high, low = [], []
    for path in iter_files():
        rel = path.relative_to(ROOT).as_posix()
        if SELF_EXEMPT.search(rel):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in BANNED.finditer(text):
            high.append(f"{rel}: 禁词 “{m.group(0)}”")
        for m in EMOJI.finditer(text):
            high.append(f"{rel}: emoji {m.group(0)!r}")
        if rel.startswith("server/app/"):
            cli_lines = _cli_main_lines(text)
            offenders = [
                i
                for i, line in enumerate(text.splitlines(), 1)
                if PRINT_IN_APP.match(line) and i not in cli_lines
            ]
            if offenders:
                high.append(
                    f"{rel}: app 层存在 print（第 {offenders[:3]} 行，CLI 入口请移至 main 或使用日志）"
                )
        todos = re.findall(r"TODO(?! T\d)", text)
        if todos:
            low.append(f"{rel}: {len(todos)} 处未带任务号的 TODO")
        for i, line in enumerate(text.splitlines(), 1):
            if len(line) > 120 and path.suffix in {".py", ".ts", ".tsx"}:
                low.append(f"{rel}:{i}: 超长行（{len(line)} 字符）")
                break  # 每文件只报首个，避免刷屏
    for item in high:
        print(f"[高危] {item}")
    for item in low:
        print(f"[提示] {item}")
    print(
        f"\n高危 {len(high)} 项 / 提示 {len(low)} 项"
        + ("——存在高危项，禁止推送" if high else "——通过")
    )
    return 1 if high else 0


if __name__ == "__main__":
    raise SystemExit(main())
