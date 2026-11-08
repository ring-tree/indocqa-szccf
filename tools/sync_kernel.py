"""内核同步：从检索问答服务的源仓库同步服务端代码到本仓库，保证本仓库自包含可复现。

用法：
    python tools/sync_kernel.py --src <检索问答服务源仓库路径>

只同步 server/ 代码（排除运行时数据与测试——语料、术语种子、测试用例
与本仓库的领域语料强绑定，由本仓库自行维护，不参与同步）。
同步后本仓库即可独立运行：python -m app.indexing.ingest ../data/corpus（在 server/ 下）
"""

import argparse
import pathlib
import shutil
import sys

SYNC_DIRS = {
    "server": ("server", {"data", ".pytest_cache", ".venv", "tests"}),
}
EXCLUDE_FILES = {".env", ".env.local", "sources.md~"}


def sync(src_root: pathlib.Path, dry_run: bool = False) -> list[str]:
    copied = []
    for rel, (src_rel, skip_dirs) in SYNC_DIRS.items():
        src = src_root / src_rel
        if not src.is_dir():
            raise SystemExit(f"源目录不存在: {src}")
        dst = pathlib.Path(rel)
        actions = _plan(src, dst, skip_dirs)
        copied.extend(f"{a[0]} -> {a[1]}" for a in actions)
        if not dry_run:
            for s, d in actions:
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s, d)
    return copied


def _plan(src: pathlib.Path, dst: pathlib.Path, skip_dirs: set) -> list:
    actions = []
    for path in sorted(src.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(src)
        if any(part in skip_dirs or part == "__pycache__" for part in rel.parts):
            continue
        if path.name in EXCLUDE_FILES or path.suffix in {".pyc", ".log"}:
            continue
        actions.append((path, dst / rel))
    return actions


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", required=True, help="内核源仓库根目录（检索问答服务仓库）")
    parser.add_argument("--dry-run", action="store_true", help="仅列出将同步的文件")
    args = parser.parse_args()
    copied = sync(pathlib.Path(args.src).resolve(), args.dry_run)
    for line in copied:
        print(line)
    print(f"共 {len(copied)} 个文件{'（dry-run，未写入）' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
