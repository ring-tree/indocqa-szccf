"""Chroma 单写保护：服务进程持有文件锁，入库/重建/删除必须经同一个写锁。

嵌入式 Chroma 不是多写服务；同一 chroma 目录被两个进程写入会让 HNSW 图漏向量。
锁文件放在 chroma 目录的上一级，`--rebuild` 删除 chroma 时不会把锁一起删掉。
"""

import os
import threading
from contextlib import contextmanager
from pathlib import Path

from app.core.exceptions import ChromaBusyError

_LOCK_NAME = ".chroma-write.lock"
_rlock = threading.RLock()
_state: dict[str, object] = {"path": None, "file": None}


def lock_path(settings) -> Path:
    return Path(settings.chroma_dir).parent / _LOCK_NAME


def _open_locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")  # noqa: SIM115 句柄由持有者保存并在释放锁时关闭
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            f.write(b"0")
            f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise
    return f


def _unlock(f) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


def acquire_server_lock(settings) -> None:
    """服务启动时调用；同一 Chroma 目录已有写者时拒绝启动。"""
    path = lock_path(settings)
    try:
        f = _open_locked(path)
    except OSError as e:
        raise ChromaBusyError(
            "知识库正在被另一个进程写入",
            detail="请先停止另一个后端实例，或为当前实例配置独立的 INDOCQA_CHROMA_DIR",
        ) from e
    _state.update(path=str(path), file=f)


def release_server_lock() -> None:
    f = _state.get("file")
    if f:
        _unlock(f)  # type: ignore[arg-type]
    _state.update(path=None, file=None)


@contextmanager
def write_guard(settings):
    """入库/重建/删除的互斥锁；同一进程内可重入，跨进程互斥。"""
    path = lock_path(settings)
    with _rlock:
        if _state.get("path") == str(path):
            yield
            return
        try:
            f = _open_locked(path)
        except OSError as e:
            raise ChromaBusyError(
                "知识库正在被另一个进程写入",
                detail="请先停止后端服务，或使用独立的 INDOCQA_CHROMA_DIR 后再入库",
            ) from e
        _state.update(path=str(path), file=f)
        try:
            yield
        finally:
            _unlock(f)
            _state.update(path=None, file=None)
