"""入库任务：后台线程执行解析/切块/向量化，提供可轮询的进度状态。

任务状态仅保存在进程内（服务重启后丢失），用于单次入库的实时进度展示；
真正的数据一致性仍由 ingest_file 的写锁与 manifest 保证。
"""

import pathlib
import threading
import time
from uuid import uuid4

from app.indexing.ingest import ingest_file

_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}
_MAX_JOBS = 50


def _cleanup_locked() -> None:
    if len(_JOBS) <= _MAX_JOBS:
        return
    items = sorted(_JOBS.items(), key=lambda item: item[1].get("createdTs", 0))
    for job_id, _ in items[: len(_JOBS) - _MAX_JOBS]:
        _JOBS.pop(job_id, None)


def create_job(total_docs: int = 1) -> str:
    job_id = uuid4().hex
    with _LOCK:
        _JOBS[job_id] = {
            "jobId": job_id,
            "status": "queued",
            "percent": 0,
            "currentDoc": 0,
            "totalDocs": total_docs,
            "message": "等待开始",
            "createdTs": time.time(),
            "result": None,
            "error": None,
        }
        _cleanup_locked()
    return job_id


def update_job(job_id: str, **fields) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            job.update(fields)


def get_job(job_id: str) -> dict | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def run_ingest_job(
    job_id: str,
    path: pathlib.Path,
    settings,
    stored_name: str,
    folder: str,
    display_name: str | None = None,
) -> None:
    def progress(stage: str, done: int, total: int) -> None:
        if stage == "parsing":
            update_job(job_id, status="running", percent=5, currentDoc=1, message="解析文档结构")
        elif stage == "chunking":
            update_job(
                job_id,
                status="running",
                percent=10,
                message=f"切分为 {done} 个检索块",
            )
        elif stage == "embedding":
            percent = 10 + int(85 * done / max(total, 1))
            update_job(
                job_id,
                status="running",
                percent=min(percent, 95),
                message=f"向量化 {done}/{total} 块",
            )

    try:
        update_job(
            job_id,
            status="running",
            percent=2,
            currentDoc=1,
            message=f"准备处理 {path.name}",
        )
        result = ingest_file(
            path,
            settings,
            stored_name,
            folder,
            progress=progress,
            display_name=display_name,
        )
        message = "内容已存在，已跳过" if result.get("duplicated") else "入库完成"
        update_job(
            job_id,
            status="done",
            percent=100,
            currentDoc=1,
            message=message,
            result=result,
        )
    except Exception as e:  # noqa: BLE001 任务失败需要回传给轮询端
        update_job(job_id, status="error", message=str(e), error=str(e))
    finally:
        path.unlink(missing_ok=True)
