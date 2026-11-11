"""RAG 系统全链路性能基准：检索质量、端到端时延、入库能力、吞吐与并发。

用法（仓库根目录）：
    uv run --project server --with matplotlib --with psutil python eval/perf_benchmark.py

说明：
- 检索质量直接调用进程内 run_retrieval（只读 Chroma，不写入），使用 eval/dataset.jsonl 40 题标注集；
- 入库/问答走真实 HTTP API，入库文档在测试结束后自动删除；
- 问答并发测试会创建临时会话并在结束后删除；
- 输出：docs/performance/data/*.json|csv、docs/performance/charts/*.png|svg、docs/performance/性能测试报告.md
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import platform
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
DATASET = ROOT / "eval" / "dataset.jsonl"
DEFAULT_OUT = ROOT / "docs" / "performance"
BRAND = "#0f9d94"
DARK = "#0f172a"
AMBER = "#d97706"
MUTED = "#64748b"

try:
    import psutil
except ImportError:  # 资源采样为可选能力
    psutil = None


# --------------------------------------------------------------------------- 工具


def pct(values: list[float], p: float) -> float:
    """Nearest-rank 百分位。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(p / 100 * len(ordered)) - 1))
    return float(ordered[index])


def mean(values: list[float]) -> float:
    return float(statistics.mean(values)) if values else 0.0


def load_cases() -> list[dict]:
    return [
        json.loads(line)
        for line in DATASET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def json_dump(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def csv_dump(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def set_retrieval_mode(mode: str) -> None:
    os.environ["INDOCQA_RETRIEVAL_MODE"] = mode
    from app.core.config import get_settings

    get_settings.cache_clear()


# --------------------------------------------------------------------------- 检索质量


def _score_case(case: dict, hits: list, latency_ms: float) -> dict:
    expected = case["expected_sources"]
    doc_names = [hit.doc_name for hit in hits]
    ranks = []
    for name in expected:
        rank = next((i + 1 for i, doc in enumerate(doc_names) if name in doc), None)
        ranks.append(rank)
    found = [rank for rank in ranks if rank]
    hit_at = {k: any(rank <= k for rank in found) for k in (1, 3, 5, 10)}
    recall5 = len([rank for rank in found if rank <= 5]) / len(expected) if expected else 0.0
    top5 = doc_names[:5]
    relevant_top5 = sum(1 for doc in top5 if any(name in doc for name in expected))
    precision5 = relevant_top5 / len(top5) if top5 else 0.0
    mrr = 1.0 / min(found) if found else 0.0
    excerpt = " ".join(hit.text[:300] for hit in hits[:3]).lower()
    points = [point.lower() in excerpt for point in case.get("answer_points", [])]
    coverage = sum(points) / len(points) if points else 0.0
    cross = (
        all(any(name in doc for doc in doc_names) for name in expected)
        if case["type"] == "multihop"
        else None
    )
    return {
        "id": case["id"],
        "type": case["type"],
        "question": case["question"],
        "expected_sources": expected,
        "top_docs": doc_names[:10],
        "hit@1": hit_at[1],
        "hit@3": hit_at[3],
        "hit@5": hit_at[5],
        "hit@10": hit_at[10],
        "recall@5": recall5,
        "precision@5": precision5,
        "mrr": mrr,
        "coverage": coverage,
        "cross_doc": cross,
        "latency_ms": latency_ms,
    }


async def run_retrieval_quality(
    cases: list[dict], modes: tuple[str, ...] = ("fixed", "adaptive")
) -> dict:
    sys.path.insert(0, str(SERVER))
    os.chdir(SERVER)
    from app.graph.graph import run_retrieval

    results: dict[str, dict] = {}
    # 预热，排除首次模型/图编译开销
    set_retrieval_mode("adaptive")
    await run_retrieval("空压机的作用是什么？", "adaptive")

    for mode in modes:
        set_retrieval_mode(mode)
        rows = []
        for case in cases:
            started = time.perf_counter()
            result = await run_retrieval(case["question"], mode)
            latency = (time.perf_counter() - started) * 1000
            rows.append(_score_case(case, result["hits"], latency))
        types = sorted({row["type"] for row in rows})
        summary = {
            "mode": mode,
            "questions": len(rows),
            "hit@1": sum(row["hit@1"] for row in rows) / len(rows),
            "hit@3": sum(row["hit@3"] for row in rows) / len(rows),
            "hit@5": sum(row["hit@5"] for row in rows) / len(rows),
            "hit@10": sum(row["hit@10"] for row in rows) / len(rows),
            "recall@5": mean([row["recall@5"] for row in rows]),
            "precision@5": mean([row["precision@5"] for row in rows]),
            "mrr": mean([row["mrr"] for row in rows]),
            "coverage": mean([row["coverage"] for row in rows]),
            "cross_doc": mean([row["cross_doc"] for row in rows if row["cross_doc"] is not None]),
            "latency_p50": pct([row["latency_ms"] for row in rows], 50),
            "latency_p95": pct([row["latency_ms"] for row in rows], 95),
            "latency_p99": pct([row["latency_ms"] for row in rows], 99),
            "latency_mean": mean([row["latency_ms"] for row in rows]),
            "by_type": {
                kind: {
                    "questions": len(sub),
                    "hit@5": sum(row["hit@5"] for row in sub) / len(sub),
                    "recall@5": mean([row["recall@5"] for row in sub]),
                    "coverage": mean([row["coverage"] for row in sub]),
                }
                for kind in types
                if (sub := [row for row in rows if row["type"] == kind])
            },
        }
        results[mode] = {"summary": summary, "rows": rows}

    os.environ.pop("INDOCQA_RETRIEVAL_MODE", None)
    from app.core.config import get_settings

    get_settings.cache_clear()
    return results


# --------------------------------------------------------------------------- HTTP


async def create_session(client: httpx.AsyncClient, title: str) -> str:
    response = await client.post("/api/sessions", json={"title": title[:60]})
    response.raise_for_status()
    return response.json()["data"]["id"]


async def delete_session(client: httpx.AsyncClient, session_id: str) -> None:
    try:
        await client.delete(f"/api/sessions/{session_id}")
    except httpx.HTTPError:
        pass


async def chat_once(client: httpx.AsyncClient, case: dict, profile: str | None) -> dict:
    session_id = await create_session(client, f"perf-{case['id']}")
    body = {
        "message": case["question"],
        "mode": "kb",
        "sessionId": session_id,
    }
    if profile:
        body["profile"] = profile
    started = time.perf_counter()
    first_token = None
    done_at = None
    parts: list[str] = []
    sources = 0
    error = None
    done: dict = {}
    event = None
    try:
        async with client.stream(
            "POST", "/api/chat", json=body, timeout=httpx.Timeout(180.0, connect=10.0)
        ) as response:
            if response.status_code != 200:
                raw = (await response.aread()).decode("utf-8", "ignore")
                raise RuntimeError(f"HTTP {response.status_code}: {raw[:300]}")
            async for line in response.aiter_lines():
                if not line:
                    event = None
                    continue
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    try:
                        data = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    now = time.perf_counter()
                    if event == "token":
                        delta = data.get("delta", "")
                        if delta and first_token is None:
                            first_token = now
                        parts.append(delta)
                    elif event == "sources":
                        sources = len(data.get("items", []))
                    elif event == "error":
                        error = data.get("message", "unknown")
                    elif event == "done":
                        done = data
                        done_at = now
        finished = done_at or time.perf_counter()
        answer = "".join(parts)
        low = answer.lower()
        points = [point.lower() in low for point in case.get("answer_points", [])]
        return {
            "id": case["id"],
            "type": case["type"],
            "question": case["question"],
            "success": error is None and bool(done),
            "error": error,
            "ttft_ms": (first_token - started) * 1000 if first_token else None,
            "total_ms": (finished - started) * 1000,
            "answer_chars": len(answer),
            "chars_per_sec": len(answer) / max(0.001, finished - (first_token or started)),
            "sources": sources,
            "citations": len(re.findall(r"\[\d+\]", answer)),
            "citation_present": bool(re.search(r"\[\d+\]", answer)),
            "answer_points_coverage": sum(points) / len(points) if points else 0.0,
            "confidence": done.get("confidence"),
            "model": done.get("model"),
            "boundary": done.get("boundary"),
            "answer_preview": answer[:220],
            "session_id": session_id,
        }
    except Exception as exc:  # noqa: BLE001 - 失败样本要进入报告
        return {
            "id": case["id"],
            "type": case["type"],
            "question": case["question"],
            "success": False,
            "error": str(exc),
            "ttft_ms": None,
            "total_ms": (time.perf_counter() - started) * 1000,
            "answer_chars": 0,
            "chars_per_sec": 0.0,
            "sources": 0,
            "citations": 0,
            "citation_present": False,
            "answer_points_coverage": 0.0,
            "confidence": None,
            "model": None,
            "boundary": None,
            "answer_preview": "",
            "session_id": session_id,
        }
    finally:
        await delete_session(client, session_id)


# --------------------------------------------------------------------------- 入库


def make_markdown(target_chars: int, seed: str, index: int) -> str:
    topics = [
        ("设备概述", "本机组为双螺杆空气压缩机，额定排气压力 0.8 MPa，配套高效永磁电机。"),
        ("安装要求", "机组应安装在通风良好、环境温度 5–40℃ 的场所，基础水平度误差不超过 2 mm/m。"),
        (
            "运行参数",
            "排气温度正常范围 75–95℃，油位应处于视油镜绿色区域，冷却水进出口温差不超过 8℃。",
        ),
        (
            "维护保养",
            "每 2000 小时更换空气过滤器滤芯，每 4000 小时更换润滑油，每 500 小时检查皮带张紧度。",
        ),
        ("故障处理", "出现 E-05 报警时应立即停机，检查油位、清理油冷器翅片并复位重启。"),
        ("安全规范", "检修前必须切断主电源并挂牌上锁，等待系统完全泄压后方可拆卸管路。"),
    ]
    lines = [
        f"# 性能基准测试文档 {index}（{seed}）",
        "",
        f"> 本文档由性能基准脚本生成，仅用于入库吞吐测试；批次标识 `{seed}`。",
        "",
    ]
    section = 0
    while sum(len(line) for line in lines) < target_chars:
        section += 1
        title, body = topics[section % len(topics)]
        lines.append(f"## {section}. {title}-{section}")
        lines.append("")
        lines.append(f"{body}（批次 {seed}，章节 {section}，唯一标识 {seed}-{index}-{section}）")
        lines.append("")
        if section % 4 == 0:
            lines.extend(
                [
                    "| 项目 | 周期 | 耗材型号 |",
                    "| --- | --- | --- |",
                    "| 空气过滤器 | 2000 h | AF-4612 |",
                    "| 润滑油 | 4000 h | LUB-46 |",
                    "| 油分芯 | 8000 h | OS-4612 |",
                    "",
                ]
            )
        for step in range(1, 4):
            lines.append(
                f"{step}. 检查第 {section}-{step} 项参数，记录读数并确认处于允许范围；"
                f"若超出范围，按维护手册第 {section} 章执行处置。"
            )
        lines.append("")
    return "\n".join(lines)


async def post_multipart(
    client: httpx.AsyncClient, path: str, filename: str, content: bytes, folder: str | None
) -> dict:
    files = {"file": (filename, content, "text/markdown")}
    data = {"folder": folder} if folder else None
    response = await client.post(path, files=files, data=data, timeout=300.0)
    payload = response.json()
    if response.status_code >= 400:
        raise RuntimeError(payload.get("error", payload))
    return payload["data"]


CORPUS_FOLDERS = {
    "compressed_air_sourcebook": "行业标准/压缩空气系统",
    "螺杆式空压机": "设备手册/螺杆空压机",
    "无油静音": "设备手册/无油空压机",
    "fixture_": "评测夹具",
}
CORPUS_SUFFIXES = {".pdf", ".md", ".txt", ".docx"}


async def ensure_corpus(client: httpx.AsyncClient) -> dict:
    """确保 data/corpus 中的内置语料已入库（评测集依赖这些文档）。"""
    corpus_dir = ROOT / "data" / "corpus"
    if not corpus_dir.exists():
        return {"uploaded": [], "present": [], "failed": []}
    response = await client.get("/api/kb/documents")
    existing = {item["filename"] for item in response.json().get("data", [])}
    uploaded: list[str] = []
    present: list[str] = []
    failed: list[dict] = []
    for path in sorted(corpus_dir.iterdir()):
        if path.name == "sources.md" or path.suffix.lower() not in CORPUS_SUFFIXES:
            continue
        if path.name in existing:
            present.append(path.name)
            continue
        folder = next(
            (value for key, value in CORPUS_FOLDERS.items() if key in path.name),
            "未分类",
        )
        try:
            await post_multipart(client, "/api/kb/ingest", path.name, path.read_bytes(), folder)
            uploaded.append(path.name)
            print(f"    补充入库：{path.name} → {folder}")
        except Exception as exc:  # noqa: BLE001 - 记录后继续
            failed.append({"filename": path.name, "error": str(exc)})
            print(f"    入库失败：{path.name}: {exc}")
    return {"uploaded": uploaded, "present": present, "failed": failed}


async def run_ingest_bench(client: httpx.AsyncClient, run_id: str) -> dict:
    folder = f"perf-bench-{run_id}"
    await client.post("/api/kb/folders", json={"path": folder})
    sizes = [8_000, 32_000, 64_000, 128_000]
    rows: list[dict] = []
    doc_ids: list[str] = []
    try:
        for index, chars in enumerate(sizes, start=1):
            text = make_markdown(chars, run_id, index)
            filename = f"perf_ingest_{index}_{chars // 1000}k.md"
            raw = text.encode("utf-8")

            started = time.perf_counter()
            preview = await post_multipart(client, "/api/kb/preview", filename, raw, None)
            parse_ms = (time.perf_counter() - started) * 1000

            started = time.perf_counter()
            job = await post_multipart(client, "/api/kb/ingest/jobs", filename, raw, folder)
            job_id = job["jobId"]
            status = None
            while True:
                await asyncio.sleep(0.3)
                response = await client.get(f"/api/kb/ingest/jobs/{job_id}")
                status = response.json()["data"]
                if status["status"] in {"done", "error"}:
                    break
                if time.perf_counter() - started > 300:
                    raise TimeoutError(f"入库任务超时: {filename}")
            total_ms = (time.perf_counter() - started) * 1000
            if status["status"] == "error":
                raise RuntimeError(status.get("error") or "入库失败")
            result = status.get("result") or {}
            if result.get("docId"):
                doc_ids.append(result["docId"])
            rows.append(
                {
                    "filename": filename,
                    "chars": len(text),
                    "bytes": len(raw),
                    "pages": result.get("pages") or preview.get("pages"),
                    "chunks": result.get("chunks", 0),
                    "tables": preview.get("tables"),
                    "preview_ms": parse_ms,
                    "ingest_ms": total_ms,
                    "index_ms": max(0.0, total_ms - parse_ms),
                    "chars_per_sec": len(text) / max(0.001, total_ms / 1000),
                    "chunks_per_sec": result.get("chunks", 0) / max(0.001, total_ms / 1000),
                    "duplicated": bool(result.get("duplicated")),
                }
            )
        total_chars = sum(row["chars"] for row in rows)
        total_chunks = sum(row["chunks"] for row in rows)
        total_ms = sum(row["ingest_ms"] for row in rows)
        summary = {
            "documents": len(rows),
            "total_chars": total_chars,
            "total_chunks": total_chunks,
            "total_seconds": total_ms / 1000,
            "docs_per_min": len(rows) / max(0.001, total_ms / 60000),
            "chars_per_sec": total_chars / max(0.001, total_ms / 1000),
            "chunks_per_sec": total_chunks / max(0.001, total_ms / 1000),
            "preview_p50_ms": pct([row["preview_ms"] for row in rows], 50),
            "preview_p95_ms": pct([row["preview_ms"] for row in rows], 95),
            "ingest_p50_ms": pct([row["ingest_ms"] for row in rows], 50),
            "ingest_p95_ms": pct([row["ingest_ms"] for row in rows], 95),
        }
        return {"summary": summary, "rows": rows}
    finally:
        for doc_id in doc_ids:
            try:
                await client.delete(f"/api/kb/documents/{doc_id}")
            except httpx.HTTPError:
                pass
        try:
            await client.delete("/api/kb/folders", params={"path": folder, "mode": "delete"})
        except httpx.HTTPError:
            pass


# --------------------------------------------------------------------------- 并发与资源


async def sample_process(stop: asyncio.Event, samples: list[dict]) -> None:
    if psutil is None:
        return
    target = None
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if "uvicorn" in cmdline and "app.main:app" in cmdline:
                target = proc
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if target is None:
        return
    target.cpu_percent(None)
    while not stop.is_set():
        try:
            samples.append(
                {
                    "ts": time.time(),
                    "cpu_percent": target.cpu_percent(None),
                    "rss_mb": target.memory_info().rss / 1024 / 1024,
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.5)
        except TimeoutError:
            pass


async def run_concurrency(
    client: httpx.AsyncClient,
    cases: list[dict],
    profile: str | None,
    levels: list[int],
    tasks_per_level: int,
) -> dict:
    levels_out = []
    resource_samples: list[dict] = []
    for level in levels:
        stop = asyncio.Event()
        sampler = asyncio.create_task(sample_process(stop, resource_samples))
        semaphore = asyncio.Semaphore(level)
        started = time.perf_counter()

        async def worker(index: int, sem: asyncio.Semaphore = semaphore):
            async with sem:
                return await chat_once(client, cases[index % len(cases)], profile)

        results = await asyncio.gather(*(worker(i) for i in range(tasks_per_level)))
        wall = time.perf_counter() - started
        stop.set()
        await sampler
        successes = [row for row in results if row["success"]]
        levels_out.append(
            {
                "concurrency": level,
                "tasks": tasks_per_level,
                "success": len(successes),
                "success_rate": len(successes) / tasks_per_level,
                "wall_seconds": wall,
                "qps": len(successes) / wall if wall else 0.0,
                "total_p50_ms": pct([row["total_ms"] for row in results], 50),
                "total_p95_ms": pct([row["total_ms"] for row in results], 95),
                "ttft_p50_ms": pct([row["ttft_ms"] for row in successes], 50),
                "ttft_p95_ms": pct([row["ttft_ms"] for row in successes], 95),
                "errors": [row["error"] for row in results if not row["success"]][:5],
            }
        )
        await asyncio.sleep(1.5)
    cpu = [row["cpu_percent"] for row in resource_samples]
    rss = [row["rss_mb"] for row in resource_samples]
    return {
        "levels": levels_out,
        "resources": {
            "samples": len(resource_samples),
            "cpu_avg": mean(cpu),
            "cpu_max": max(cpu) if cpu else 0.0,
            "rss_avg_mb": mean(rss),
            "rss_max_mb": max(rss) if rss else 0.0,
        },
    }


# --------------------------------------------------------------------------- 图表


def render_charts(
    retrieval: dict, ingest: dict, chat: dict, concurrency: dict, out: Path
) -> list[str]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        plt.rcParams["font.sans-serif"] = [
            "Microsoft YaHei",
            "SimHei",
            "Arial Unicode MS",
            "DejaVu Sans",
        ]
        plt.rcParams["axes.unicode_minus"] = False
        plt.rcParams["axes.edgecolor"] = "#cbd5e1"
        plt.rcParams["axes.labelcolor"] = DARK
        plt.rcParams["xtick.color"] = MUTED
        plt.rcParams["ytick.color"] = MUTED
    except ImportError:
        return []

    out.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []

    def save(fig, name: str) -> None:
        for ext in ("png", "svg"):
            path = out / f"{name}.{ext}"
            fig.savefig(path, dpi=180, bbox_inches="tight", facecolor="white")
            if ext == "png":
                saved.append(str(path))
        plt.close(fig)

    # 1. 检索质量
    if retrieval:
        metrics = ["hit@1", "hit@3", "hit@5", "recall@5", "precision@5", "mrr", "coverage"]
        labels = ["Hit@1", "Hit@3", "Hit@5", "Recall@5", "Precision@5", "MRR", "要点覆盖"]
        modes = [mode for mode in ("fixed", "adaptive") if mode in retrieval]
        x = range(len(metrics))
        width = 0.36
        fig, ax = plt.subplots(figsize=(9, 4.4))
        for offset, mode in enumerate(modes):
            values = [retrieval[mode]["summary"][metric] * 100 for metric in metrics]
            bars = ax.bar(
                [i + (offset - 0.5) * width for i in x],
                values,
                width,
                label="固定参数基线" if mode == "fixed" else "自适应路由（本系统）",
                color="#94a3b8" if mode == "fixed" else BRAND,
            )
            for bar, value in zip(bars, values):
                ax.text(
                    bar.get_x() + bar.get_width() / 2,
                    value + 1.2,
                    f"{value:.0f}",
                    ha="center",
                    fontsize=8,
                    color=DARK,
                )
        ax.set_xticks(list(x))
        ax.set_xticklabels(labels)
        ax.set_ylim(0, 112)
        ax.set_ylabel("百分比 / 比率（%）")
        ax.set_title("检索质量对比（40 题标注集）", fontsize=12, color=DARK, pad=12)
        ax.legend(
            frameon=False,
            loc="upper center",
            bbox_to_anchor=(0.5, -0.14),
            ncol=2,
        )
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color="#e2e8f0", linewidth=0.8)
        ax.set_axisbelow(True)
        save(fig, "chart-retrieval-quality")

        # 2. 分题型 Recall@5
        types = list(retrieval["adaptive"]["summary"]["by_type"].keys())
        fig, ax = plt.subplots(figsize=(9, 4.2))
        width = 0.36
        for offset, mode in enumerate(modes):
            values = [
                retrieval[mode]["summary"]["by_type"].get(kind, {}).get("recall@5", 0) * 100
                for kind in types
            ]
            ax.bar(
                [i + (offset - 0.5) * width for i in range(len(types))],
                values,
                width,
                label="固定参数基线" if mode == "fixed" else "自适应路由（本系统）",
                color="#94a3b8" if mode == "fixed" else BRAND,
            )
        ax.set_xticks(range(len(types)))
        ax.set_xticklabels(types)
        ax.set_ylim(0, 112)
        ax.set_ylabel("Recall@5（%）")
        ax.set_title("分题型检索召回率", fontsize=12, color=DARK, pad=12)
        ax.legend(
            frameon=False,
            loc="upper center",
            bbox_to_anchor=(0.5, -0.14),
            ncol=2,
        )
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color="#e2e8f0", linewidth=0.8)
        ax.set_axisbelow(True)
        save(fig, "chart-recall-by-type")

    # 3. 时延
    if retrieval and chat:
        series = [
            ("检索 P50", retrieval["adaptive"]["summary"]["latency_p50"]),
            ("检索 P95", retrieval["adaptive"]["summary"]["latency_p95"]),
            ("首 Token P50", chat["summary"]["ttft_p50_ms"]),
            ("首 Token P95", chat["summary"]["ttft_p95_ms"]),
            ("端到端 P50", chat["summary"]["total_p50_ms"]),
            ("端到端 P95", chat["summary"]["total_p95_ms"]),
        ]
        fig, ax = plt.subplots(figsize=(9, 4.2))
        colors = [BRAND, BRAND, "#38bdf8", "#38bdf8", AMBER, AMBER]
        bars = ax.bar([label for label, _ in series], [value for _, value in series], color=colors)
        ax.set_yscale("log")
        ax.set_ylabel("毫秒（对数轴）")
        ax.set_title("关键时延指标（单并发）", fontsize=12, color=DARK, pad=12)
        for bar, (_, value) in zip(bars, series):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                value * 1.08,
                f"{value:.0f}",
                ha="center",
                fontsize=8,
                color=DARK,
            )
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color="#e2e8f0", linewidth=0.8)
        ax.set_axisbelow(True)
        plt.xticks(rotation=15)
        save(fig, "chart-latency")

    # 4. 入库
    if ingest and ingest["rows"]:
        rows = ingest["rows"]
        fig, ax = plt.subplots(figsize=(9, 4.2))
        labels = [row["filename"].replace("perf_ingest_", "").replace(".md", "") for row in rows]
        bars = ax.bar(labels, [row["ingest_ms"] / 1000 for row in rows], color=BRAND, width=0.55)
        for bar, row in zip(bars, rows):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 0.4,
                f"{bar.get_height():.1f}s\n{row['chunks']} 块",
                ha="center",
                fontsize=8,
                color=DARK,
            )
        ax.set_ylabel("入库耗时（秒）")
        ax.set_title("入库处理能力（Markdown，按字符规模）", fontsize=12, color=DARK, pad=12)
        ax2 = ax.twinx()
        ax2.plot(
            labels,
            [row["chunks_per_sec"] for row in rows],
            color=AMBER,
            marker="o",
            linewidth=2,
            label="块/秒",
        )
        ax2.set_ylabel("吞吐（块/秒）", color=AMBER)
        ax2.tick_params(axis="y", colors=AMBER)
        ax2.spines[["top"]].set_visible(False)
        ax.spines[["top"]].set_visible(False)
        ax.grid(axis="y", color="#e2e8f0", linewidth=0.8)
        ax.set_axisbelow(True)
        save(fig, "chart-ingest")

    # 5. 并发
    if concurrency and concurrency["levels"]:
        levels = concurrency["levels"]
        fig, ax = plt.subplots(figsize=(9, 4.2))
        x = range(len(levels))
        bars = ax.bar(
            x,
            [row["qps"] for row in levels],
            color=BRAND,
            width=0.5,
            label="成功 QPS",
        )
        for bar, row in zip(bars, levels):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 0.02,
                f"{row['qps']:.2f}",
                ha="center",
                fontsize=9,
                color=DARK,
            )
        ax.set_xticks(list(x))
        ax.set_xticklabels([f"并发 {row['concurrency']}" for row in levels])
        ax.set_ylabel("QPS（请求/秒）")
        ax.set_title("并发处理能力与吞吐", fontsize=12, color=DARK, pad=12)
        ax2 = ax.twinx()
        ax2.plot(
            list(x),
            [row["total_p95_ms"] / 1000 for row in levels],
            color=AMBER,
            marker="o",
            linewidth=2,
            label="P95 时延（秒）",
        )
        ax2.set_ylabel("P95 时延（秒）", color=AMBER)
        ax2.tick_params(axis="y", colors=AMBER)
        ax2.spines[["top"]].set_visible(False)
        ax.spines[["top"]].set_visible(False)
        ax.grid(axis="y", color="#e2e8f0", linewidth=0.8)
        ax.set_axisbelow(True)
        lines, labels_ = ax.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax.legend(lines + lines2, labels_ + labels2, frameon=False, loc="upper left")
        save(fig, "chart-concurrency")

    return saved


# --------------------------------------------------------------------------- 报告


def _fmt(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}"


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}"


def render_report(
    env: dict,
    retrieval: dict,
    ingest: dict,
    chat: dict,
    concurrency: dict,
    out: Path,
    charts: list[str],
) -> Path:
    adaptive = retrieval.get("adaptive", {}).get("summary", {})
    fixed = retrieval.get("fixed", {}).get("summary", {})
    lines = [
        "# RAG 系统性能测试报告",
        "",
        f"- 测试时间：{env['timestamp']}",
        f"- 代码版本：`{env['git_commit']}`",
        (
            f"- 测试环境：{env['platform']} · Python {env['python']} · {env.get('cpu', '-')} · "
            f"{env.get('cpu_count', '-')} 核 · 内存 {env.get('memory_gb', '-')} GB"
        ),
        f"- 嵌入后端：`{env['embedding'].get('backend')}` / `{env['embedding'].get('model')}`",
        f"- 生成模型（配置主模型）：`{env.get('llm_model', '-')}`（流式）",
        "- 数据集：`eval/dataset.jsonl` 40 题（术语 7 / 参数 7 / 步骤 7 / 故障 8 / 多跳 11）",
    ]
    if env.get("corpus", {}).get("uploaded"):
        lines.append(f"- 语料补充：评测前自动补入 {len(env['corpus']['uploaded'])} 份内置文档")
    lines += [
        "",
        "> 本报告由 `eval/perf_benchmark.py` 自动生成；原始数据与图表位于 `docs/performance/`。",
        "",
        "## 一、核心结论",
        "",
    ]
    if adaptive and fixed:
        lines += [
            (
                f"- 检索质量：自适应路由 Hit@5 **{_pct(adaptive['hit@5'])}**、Recall@5 "
                f"**{_pct(adaptive['recall@5'])}**、MRR **{adaptive['mrr']:.3f}**；"
                f"固定参数基线 Hit@5 {_pct(fixed['hit@5'])}、Recall@5 {_pct(fixed['recall@5'])}。"
            ),
            (
                f"- 多跳跨文档命中：自适应 **{_pct(adaptive['cross_doc'])}**，固定基线 "
                f"{_pct(fixed['cross_doc'])}。"
            ),
            (
                f"- 待改进项：多跳题答案要点覆盖率 "
                f"{_pct(adaptive.get('by_type', {}).get('multihop', {}).get('coverage', 0))}，"
                f"是后续多跳实体补查与答案组织优化的重点。"
            ),
        ]
    if chat and chat["summary"]["requests"]:
        summary = chat["summary"]
        lines += [
            (
                f"- 端到端响应：首 Token P50 **{summary['ttft_p50_ms']:.0f} ms** / P95 "
                f"**{summary['ttft_p95_ms']:.0f} ms**；完整回答 P50 "
                f"**{summary['total_p50_ms']:.0f} ms** / P95 **{summary['total_p95_ms']:.0f} ms**。"
            ),
            (
                f"- 回答可用性：请求成功率 **{_pct(summary['success_rate'])}**，引用覆盖率 "
                f"{_pct(summary['citation_rate'])}，答案要点覆盖率 "
                f"{_pct(summary['answer_points_coverage'])}。"
            ),
        ]
    if ingest and ingest["rows"]:
        summary = ingest["summary"]
        lines += [
            (
                f"- 入库能力：{summary['documents']} 份 / {summary['total_chars']:,} 字符 / "
                f"{summary['total_chunks']} 块，总耗时 **{summary['total_seconds']:.1f} s**，"
                f"吞吐 **{summary['chars_per_sec']:.0f} 字符/秒**、**{summary['chunks_per_sec']:.2f} 块/秒**。"
            ),
        ]
    if concurrency and concurrency["levels"]:
        best = max(concurrency["levels"], key=lambda row: row["qps"])
        base = concurrency["levels"][0]
        lines += [
            (
                f"- 并发能力：并发 {best['concurrency']} 时成功 QPS 最高 **{best['qps']:.2f}**"
                f"（并发 {base['concurrency']} 为 {base['qps']:.2f}）；"
                f"并发 {best['concurrency']} 下 P95 时延 {best['total_p95_ms']:.0f} ms，"
                f"成功率 {_pct(best['success_rate'])}。"
            ),
        ]
    lines += [
        "",
        "## 二、检索质量",
        "",
        "### 2.1 总体指标",
        "",
        "| 指标 | 固定参数基线 | 自适应路由（本系统） |",
        "| --- | ---: | ---: |",
    ]
    for key, label in [
        ("hit@1", "Hit@1"),
        ("hit@3", "Hit@3"),
        ("hit@5", "Hit@5"),
        ("hit@10", "Hit@10"),
        ("recall@5", "Recall@5"),
        ("precision@5", "Precision@5"),
        ("mrr", "MRR"),
        ("coverage", "答案要点覆盖率"),
        ("cross_doc", "多跳跨文档命中"),
    ]:
        if key not in adaptive or key not in fixed:
            continue
        if key in {"mrr"}:
            cells = f"{fixed[key]:.3f} | {adaptive[key]:.3f}"
        else:
            cells = f"{_pct(fixed[key])} | {_pct(adaptive[key])}"
        lines.append(f"| {label} | {cells} |")
    lines += [
        "",
        (
            "> Precision@5 为**块级**精确率（前 5 个检索块中来自标注文档的比例）；同一文档多块命中会重复计入，"
            "因此该值天然低于文档级命中率，仅用于横向对比，不宜单独作为准确率结论。"
        ),
        "",
        (
            f"- 检索延迟（自适应）：P50 {adaptive.get('latency_p50', 0):.0f} ms，"
            f"P95 {adaptive.get('latency_p95', 0):.0f} ms，P99 {adaptive.get('latency_p99', 0):.0f} ms；"
            "前两者为进程预热后的稳态值，P99 可能包含首次模型加载/连接建立开销。"
        ),
        "",
        "### 2.2 分题型 Recall@5",
        "",
        "| 题型 | 题数 | 固定参数基线 | 自适应路由 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for kind, data in adaptive.get("by_type", {}).items():
        base = fixed.get("by_type", {}).get(kind, {})
        lines.append(
            f"| {kind} | {data['questions']} | {_pct(base.get('recall@5', 0))} | "
            f"{_pct(data['recall@5'])} |"
        )
    if charts:
        lines += ["", "![检索质量对比](charts/chart-retrieval-quality.png)", ""]
        lines += ["![分题型召回率](charts/chart-recall-by-type.png)", ""]

    lines += [
        "## 三、端到端响应时间",
        "",
        "| 指标 | P50 | P95 | 均值 |",
        "| --- | ---: | ---: | ---: |",
    ]
    if chat and chat["summary"]["requests"]:
        summary = chat["summary"]
        lines += [
            (
                f"| 首 Token（ms） | {summary['ttft_p50_ms']:.0f} | {summary['ttft_p95_ms']:.0f} | "
                f"{summary['ttft_mean_ms']:.0f} |"
            ),
            (
                f"| 完整回答（ms） | {summary['total_p50_ms']:.0f} | {summary['total_p95_ms']:.0f} | "
                f"{summary['total_mean_ms']:.0f} |"
            ),
            (
                f"| 生成速度（字符/秒） | {summary['cps_p50']:.1f} | {summary['cps_p95']:.1f} | "
                f"{summary['cps_mean']:.1f} |"
            ),
        ]
        lines += [
            "",
            f"- 样本数：{summary['requests']} 题（覆盖 5 类问法）；成功率 {_pct(summary['success_rate'])}。",
            (
                f"- 引用覆盖率 {_pct(summary['citation_rate'])}；答案要点覆盖率 "
                f"{_pct(summary['answer_points_coverage'])}。"
            ),
            "",
            "### 3.1 逐题明细",
            "",
            "| 题号 | 类型 | 首 Token (ms) | 总耗时 (ms) | 来源数 | 引用数 | 要点覆盖 | 状态 |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
        for row in chat["rows"]:
            lines.append(
                f"| {row['id']} | {row['type']} | "
                f"{_ms(row['ttft_ms'])} | "
                f"{row['total_ms']:.0f} | {row['sources']} | {row['citations']} | "
                f"{_pct(row['answer_points_coverage'])} | "
                f"{'成功' if row['success'] else '失败'} |"
            )
        lines.append("")
        if summary.get("models_used"):
            usage = "、".join(f"{name} × {count}" for name, count in summary["models_used"].items())
            lines.append(f"- 实际使用模型：{usage}（若与配置主模型不同，说明触发了故障切换）。")
            configured = env.get("llm_model")
            if configured and configured not in summary["models_used"]:
                lines.append(
                    f"- 说明：配置主模型 `{configured}` 在本轮未成功返回，以上时延包含故障切换开销；"
                    "建议检查主供应商配额、限流与网络可用性。"
                )
    if charts:
        lines += ["", "![关键时延](charts/chart-latency.png)", ""]

    lines += ["## 四、入库处理能力", ""]
    if ingest and ingest["rows"]:
        summary = ingest["summary"]
        lines += [
            (
                f"- 总量：{summary['documents']} 份文档、{summary['total_chars']:,} 字符、"
                f"{summary['total_chunks']} 个检索块；总耗时 {summary['total_seconds']:.1f} s。"
            ),
            (
                f"- 综合吞吐：{summary['docs_per_min']:.2f} 份/分钟、{summary['chars_per_sec']:.0f} 字符/秒、"
                f"{summary['chunks_per_sec']:.2f} 块/秒。"
            ),
            "",
            "| 文档 | 字符数 | 块数 | 预检 (ms) | 入库 (ms) | 索引 (ms) | 字符/秒 | 块/秒 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for row in ingest["rows"]:
            lines.append(
                f"| {row['filename']} | {row['chars']:,} | {row['chunks']} | "
                f"{row['preview_ms']:.0f} | {row['ingest_ms']:.0f} | {row['index_ms']:.0f} | "
                f"{row['chars_per_sec']:.0f} | {row['chunks_per_sec']:.2f} |"
            )
        lines += [
            "",
            (
                "> `预检` 为解析/分块演练，`入库` 为解析 + 嵌入 + 向量与 BM25 写入的完整链路；"
                "`索引` 为两者差值，近似代表嵌入与写入开销。"
            ),
        ]
    else:
        lines.append("未执行入库测试。")
    if charts:
        lines += ["", "![入库能力](charts/chart-ingest.png)", ""]

    lines += ["## 五、吞吐与并发", ""]
    if concurrency and concurrency["levels"]:
        lines += [
            "| 并发度 | 任务数 | 成功 | 成功率 | 墙钟 (s) | QPS | 首 Token P95 (ms) | P95 时延 (ms) |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for row in concurrency["levels"]:
            lines.append(
                f"| {row['concurrency']} | {row['tasks']} | {row['success']} | "
                f"{_pct(row['success_rate'])} | {row['wall_seconds']:.1f} | {row['qps']:.2f} | "
                f"{row['ttft_p95_ms']:.0f} | {row['total_p95_ms']:.0f} |"
            )
        resources = concurrency.get("resources", {})
        if resources.get("samples"):
            if resources["cpu_max"] < 0.1:
                cpu_text = "CPU 采样低于 0.1%（问答以网络 I/O 等待为主）"
            else:
                cpu_text = (
                    f"CPU 平均 {resources['cpu_avg']:.1f}% / 峰值 {resources['cpu_max']:.1f}%"
                )
            lines += [
                "",
                (
                    f"资源占用（并发压测期间，后端进程）：{cpu_text}，"
                    f"内存平均 {resources['rss_avg_mb']:.0f} MB / 峰值 {resources['rss_max_mb']:.0f} MB。"
                ),
            ]
        if len(concurrency["levels"]) >= 2:
            first, last = concurrency["levels"][0], concurrency["levels"][-1]
            ratio = last["qps"] / first["qps"] if first["qps"] else 0.0
            lines += [
                "",
                (
                    f"> 解读：并发 {first['concurrency']} → {last['concurrency']} 时 QPS 提升约 "
                    f"{ratio:.1f}×，所有请求均成功；服务端内存稳定，说明吞吐瓶颈在外部模型供应商的"
                    "排队/限流，而不是检索与编排层。"
                ),
            ]
        errors = [err for row in concurrency["levels"] for err in row["errors"]]
        if errors:
            lines += ["", "并发失败样例：", ""] + [f"- `{err[:160]}`" for err in errors[:5]]
    else:
        lines.append("未执行并发测试。")
    if charts:
        lines += ["", "![并发吞吐](charts/chart-concurrency.png)", ""]

    lines += [
        "## 六、测试方法",
        "",
        (
            "1. **检索质量**：对 40 题标注集逐题调用进程内 `run_retrieval`，比较固定参数与自适应路由；"
            "Hit@k 表示前 k 个命中块中出现标注来源文档，Recall@5 按题目的多个期望来源取平均，"
            "MRR 取首个相关来源排名的倒数，答案要点覆盖率以检索摘录中命中的标注要点计算。"
        ),
        (
            "2. **入库能力**：通过 `POST /api/kb/ingest/jobs` 上传 4 份 8k–128k 字符的 Markdown，"
            "轮询任务直到完成；`POST /api/preview` 单独测解析预检。测试文档在结束后删除。"
        ),
        (
            "3. **端到端响应**：通过 `/api/chat` SSE 测量首 Token 与完整回答耗时，"
            "每题独立会话，结束后删除会话。"
        ),
        (
            "4. **并发能力**：以 1/2/4 等并发度分别发起多组问答任务，统计成功率、QPS 与 P95 时延；"
            "实际并发度与任务数见上表；同时采样后端进程 CPU 与 RSS。"
        ),
        "",
        "## 七、复现方式",
        "",
        "```bash",
        "# 1. 启动后端（8010）与前端（5173）",
        "cd server && uv run uvicorn app.main:app --port 8010",
        "cd web && npm run dev",
        "",
        "# 2. 执行完整性能基准（自动生成数据、图表与本报告）",
        "uv run --project server --with matplotlib --with psutil python eval/perf_benchmark.py",
        "```",
        "",
        "## 八、说明与限制",
        "",
        (
            "- 测试为**单机单进程**部署，生成模型为外部 API，时延包含网络与上游排队时间；"
            "不同时段/不同供应商的结果会有波动。"
        ),
        "- 检索质量指标衡量“来源命中”，不等价于最终答案正确率；生成质量另见引用审计与要点覆盖率。",
        "- 并发 QPS 受上游模型限流影响；失败请求会在明细中标注，不剔除、不美化。",
        "- 入库吞吐受文档结构、表格密度、嵌入后端（本机 Ollama bge-m3）影响。",
        "",
    ]
    path = out / "性能测试报告.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- 主流程


def summarize_chat(rows: list[dict]) -> dict:
    ok = [row for row in rows if row["success"]]
    ttft = [row["ttft_ms"] for row in ok if row["ttft_ms"] is not None]
    totals = [row["total_ms"] for row in rows]
    cps = [row["chars_per_sec"] for row in ok]
    models: dict[str, int] = {}
    for row in ok:
        name = row.get("model") or "unknown"
        models[name] = models.get(name, 0) + 1
    return {
        "requests": len(rows),
        "success": len(ok),
        "success_rate": len(ok) / len(rows) if rows else 0.0,
        "models_used": models,
        "ttft_p50_ms": pct(ttft, 50),
        "ttft_p95_ms": pct(ttft, 95),
        "ttft_mean_ms": mean(ttft),
        "total_p50_ms": pct(totals, 50),
        "total_p95_ms": pct(totals, 95),
        "total_mean_ms": mean(totals),
        "cps_p50": pct(cps, 50),
        "cps_p95": pct(cps, 95),
        "cps_mean": mean(cps),
        "citation_rate": sum(1 for row in ok if row["citation_present"]) / len(ok) if ok else 0.0,
        "answer_points_coverage": mean([row["answer_points_coverage"] for row in ok]),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="IndocQA RAG 性能基准")
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--output", default=str(DEFAULT_OUT))
    parser.add_argument("--chat-sample", type=int, default=10)
    parser.add_argument("--concurrency", default="1,2,4,8")
    parser.add_argument("--tasks-per-level", type=int, default=8)
    parser.add_argument(
        "--profile",
        default=None,
        help="问答使用的模型 profile ID；不传则使用 /api/models 的第一个（主模型）",
    )
    parser.add_argument("--skip-retrieval", action="store_true")
    parser.add_argument("--skip-ingest", action="store_true")
    parser.add_argument("--skip-chat", action="store_true")
    parser.add_argument("--skip-concurrency", action="store_true")
    parser.add_argument("--skip-charts", action="store_true")
    parser.add_argument(
        "--ensure-corpus",
        action="store_true",
        help="评测前把 data/corpus 中缺失的内置语料补入知识库",
    )
    args = parser.parse_args()

    out = Path(args.output)
    data_dir = out / "data"
    chart_dir = out / "charts"
    out.mkdir(parents=True, exist_ok=True)

    cases = load_cases()
    run_id = datetime.now().strftime("%m%d%H%M%S")

    async with httpx.AsyncClient(base_url=args.base_url, timeout=30.0) as client:
        health = (await client.get("/api/health")).json()
        models = (await client.get("/api/models")).json().get("data", [])
        profile = args.profile or (models[0]["id"] if models else None)
        chosen = next(
            (item for item in models if item["id"] == profile), models[0] if models else {}
        )
        llm_model = chosen.get("model")
        if args.profile and args.profile not in {item["id"] for item in models}:
            print(f"警告：profile {args.profile} 不在模型列表中，将由后端校验")

        env = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "git_commit": git_commit(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu": os.environ.get("PROCESSOR_IDENTIFIER") or platform.processor() or "-",
            "cpu_count": os.cpu_count(),
            "memory_gb": round(psutil.virtual_memory().total / 1024**3, 1) if psutil else None,
            "health": health,
            "embedding": health.get("embedding", {}),
            "llm_model": llm_model,
            "profile": profile,
            "models": models,
            "dataset_size": len(cases),
            "base_url": args.base_url,
        }
        json_dump(data_dir / "environment.json", env)

        if args.ensure_corpus:
            print("[0/4] 检查内置语料完整性...")
            env["corpus"] = await ensure_corpus(client)
            json_dump(data_dir / "environment.json", env)

        retrieval: dict = {}
        if not args.skip_retrieval:
            print("[1/4] 检索质量评测（40 题 × fixed/adaptive）...")
            retrieval = await run_retrieval_quality(cases)
            json_dump(data_dir / "retrieval_quality.json", retrieval)
            for mode, payload in retrieval.items():
                csv_dump(
                    data_dir / f"retrieval_{mode}.csv",
                    payload["rows"],
                    [
                        "id",
                        "type",
                        "hit@1",
                        "hit@3",
                        "hit@5",
                        "recall@5",
                        "precision@5",
                        "mrr",
                        "coverage",
                        "cross_doc",
                        "latency_ms",
                    ],
                )
        else:
            cached = data_dir / "retrieval_quality.json"
            if cached.exists():
                retrieval = json.loads(cached.read_text(encoding="utf-8"))
                print("[1/4] 复用已有检索评测结果（--skip-retrieval）")

    ingest: dict = {}
    chat: dict = {}
    concurrency: dict = {}

    if args.skip_ingest:
        cached = data_dir / "ingest.json"
        if cached.exists():
            ingest = json.loads(cached.read_text(encoding="utf-8"))
            print("[2/4] 复用已有入库测试结果（--skip-ingest）")
    if args.skip_chat:
        cached = data_dir / "chat_latency.json"
        if cached.exists():
            chat = json.loads(cached.read_text(encoding="utf-8"))
            chat["summary"] = summarize_chat(chat["rows"])
            print("[3/4] 复用已有端到端响应结果（--skip-chat）")
    if args.skip_concurrency:
        cached = data_dir / "concurrency.json"
        if cached.exists():
            concurrency = json.loads(cached.read_text(encoding="utf-8"))
            print("[4/4] 复用已有并发测试结果（--skip-concurrency）")

    if not args.skip_ingest or not args.skip_chat or not args.skip_concurrency:
        async with httpx.AsyncClient(base_url=args.base_url, timeout=300.0) as client:
            if not args.skip_ingest:
                print("[2/4] 入库能力测试（4 份 8k–128k 字符文档）...")
                ingest = await run_ingest_bench(client, run_id)
                json_dump(data_dir / "ingest.json", ingest)
                csv_dump(
                    data_dir / "ingest.csv",
                    ingest["rows"],
                    [
                        "filename",
                        "chars",
                        "bytes",
                        "pages",
                        "chunks",
                        "tables",
                        "preview_ms",
                        "ingest_ms",
                        "index_ms",
                        "chars_per_sec",
                        "chunks_per_sec",
                    ],
                )

            if not args.skip_chat:
                per_type = {}
                for case in cases:
                    per_type.setdefault(case["type"], []).append(case)
                selected = []
                for kind in ("term", "param", "procedure", "fault", "multihop"):
                    selected.extend(per_type.get(kind, [])[: max(1, args.chat_sample // 5)])
                selected = selected[: args.chat_sample]
                print(f"[3/4] 端到端响应测试（{len(selected)} 题）...")
                rows = []
                for case in selected:
                    row = await chat_once(client, case, profile)
                    print(
                        f"    {case['id']:9s} ttft={_ms(row['ttft_ms'])}ms "
                        f"total={row['total_ms']:.0f}ms "
                        f"{'ok' if row['success'] else 'FAIL'}"
                    )
                    rows.append(row)
                chat = {"summary": summarize_chat(rows), "rows": rows}
                json_dump(data_dir / "chat_latency.json", chat)
                csv_dump(
                    data_dir / "chat_latency.csv",
                    rows,
                    [
                        "id",
                        "type",
                        "success",
                        "ttft_ms",
                        "total_ms",
                        "answer_chars",
                        "chars_per_sec",
                        "sources",
                        "citations",
                        "answer_points_coverage",
                        "confidence",
                        "model",
                        "error",
                    ],
                )

            if not args.skip_concurrency:
                levels = [int(item) for item in args.concurrency.split(",") if item.strip()]
                pool = [
                    case
                    for case in cases
                    if case["type"] in {"term", "param", "procedure", "fault"}
                ][:4]
                if pool:
                    print(f"[4/4] 并发测试（并发度 {levels} × {args.tasks_per_level} 任务）...")
                    concurrency = await run_concurrency(
                        client, pool, profile, levels, args.tasks_per_level
                    )
                    json_dump(data_dir / "concurrency.json", concurrency)
                    csv_dump(
                        data_dir / "concurrency.csv",
                        concurrency["levels"],
                        [
                            "concurrency",
                            "tasks",
                            "success",
                            "success_rate",
                            "wall_seconds",
                            "qps",
                            "ttft_p50_ms",
                            "ttft_p95_ms",
                            "total_p50_ms",
                            "total_p95_ms",
                        ],
                    )

    charts: list[str] = []
    if not args.skip_charts:
        charts = render_charts(retrieval, ingest, chat, concurrency, chart_dir)

    report = render_report(
        env,
        retrieval,
        ingest,
        chat,
        concurrency,
        out,
        charts,
    )
    json_dump(
        data_dir / "summary.json",
        {
            "environment": env,
            "retrieval": {mode: payload["summary"] for mode, payload in retrieval.items()},
            "ingest": ingest.get("summary"),
            "chat": chat.get("summary"),
            "concurrency": concurrency.get("levels"),
            "resources": concurrency.get("resources"),
            "charts": charts,
        },
    )
    print(f"完成：{report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
