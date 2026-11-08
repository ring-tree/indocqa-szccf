"""运行方案矩阵测试：嵌入后端 × 生成模式 × 检索模式 全组合实测。

用法：python scripts/runtime_matrix.py（自动读取同目录 runtime_questions.json）
产物：docs/evidence/runtime-matrix-<时间戳>.md
每组合统计：冷启动首token / 稳态首token p50 / 总耗时 p50 / 来源数 / 置信度分布 / 要点命中。
"""

import asyncio
import json
import os
import pathlib
import statistics
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

LLM_VARS = (
    "INDOCQA_LLM_BASE_URL", "INDOCQA_LLM_API_KEY", "INDOCQA_LLM_MODEL",
    "INDOCQA_LLM_BACKUP_BASE_URL", "INDOCQA_LLM_BACKUP_API_KEY", "INDOCQA_LLM_BACKUP_MODEL",
)


def apply_combo(backend: str, chroma: str, llm: bool, mode: str) -> None:
    from app.core.config import get_settings

    for key in list(k for k in os.environ if k.startswith("INDOCQA_")):
        os.environ.pop(key, None)
    os.environ["INDOCQA_EMBED_BACKEND"] = backend
    os.environ["INDOCQA_CHROMA_DIR"] = chroma
    os.environ["INDOCQA_RETRIEVAL_MODE"] = mode
    if not llm:
        for var in LLM_VARS:
            os.environ[var] = ""
    get_settings.cache_clear()


async def ask_once(question: str) -> dict:
    from app.graph.pipeline import chat_stream

    t0 = time.perf_counter()
    first = None
    tokens, sources, done, traces = [], [], None, []
    async for ev in chat_stream(question, "kb"):
        el = (time.perf_counter() - t0) * 1000
        if ev["event"] == "token":
            if first is None:
                first = el
            tokens.append(ev["data"]["delta"])
        elif ev["event"] == "sources":
            sources = ev["data"]["items"]
        elif ev["event"] == "done":
            done = ev["data"]
        elif ev["event"] == "trace":
            traces.append(ev["data"])
    answer = "".join(tokens)
    cited = {int(n) for n in __import__("re").findall(r"\[(\d+)\]", answer)}
    return {
        "first_ms": first,
        "total_ms": (time.perf_counter() - t0) * 1000,
        "n_sources": len(sources),
        "confidence": done.get("confidence") if done else None,
        "cited": bool(cited & set(range(1, len(sources) + 1))) if sources else False,
        "answer": answer,
        "refused": "未覆盖" in answer[:30],
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    os.chdir(ROOT / "server")
    questions = json.loads(
        (pathlib.Path(__file__).parent / "runtime_questions.json").read_text(encoding="utf-8")
    )

    combos = [
        {"name": "local+生成", "backend": "local", "chroma": "data/chroma-local", "llm": True, "mode": "adaptive", "subset": None},
        {"name": "local+摘录", "backend": "local", "chroma": "data/chroma-local", "llm": False, "mode": "adaptive", "subset": None},
        {"name": "ollama+生成", "backend": "ollama", "chroma": "data/chroma", "llm": True, "mode": "adaptive", "subset": None},
        {"name": "ollama+生成+fixed基线", "backend": "ollama", "chroma": "data/chroma", "llm": True, "mode": "fixed", "subset": ["term", "param", "multihop"]},
        {"name": "auto探测(→ollama)", "backend": "auto", "chroma": "data/chroma", "llm": True, "mode": "adaptive", "subset": ["term"]},
        {"name": "minilm通路(小库)", "backend": "minilm", "chroma": "data/chroma-minilm", "llm": True, "mode": "adaptive", "subset": ["param"]},
    ]

    results = {}
    for combo in combos:
        name = combo["name"]
        qs = [q for q in questions if combo["subset"] is None or q["type"] in combo["subset"]]
        print(f"[{name}] {len(qs)} 题 ...", flush=True)
        apply_combo(combo["backend"], combo["chroma"], combo["llm"], combo["mode"])
        rows = []
        for i, q in enumerate(qs):
            r = asyncio.run(ask_once(q["question"]))
            hit = any(p.lower() in r["answer"].lower() for p in q.get("points", [])) if q.get("points") else None
            if q["type"] == "refuse":
                hit = r["refused"]
            rows.append({"q": q, "run": r, "hit": hit})
            tag = "" if hit is None else ("Y" if hit else "N")
            print(f"    {q['type']:9s} 首token={r['first_ms'] and int(r['first_ms'])}ms 总={int(r['total_ms'])}ms 源={r['n_sources']} conf={r['confidence']} {tag}", flush=True)
        results[name] = rows

    lines = [
        "# 运行方案矩阵实测",
        "",
        f"- 日期：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 题库：{len(questions)} 题（{ '/'.join(q['type'] for q in questions) }）",
        "",
        "| 组合 | 题数 | 首token p50 (ms) | 总耗时 p50 (ms) | 平均来源数 | 要点命中 | 高/中/低置信 |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, rows in results.items():
        runs = [r["run"] for r in rows]
        firsts = [r["first_ms"] for r in runs if r["first_ms"]]
        totals = [r["total_ms"] for r in runs]
        hits = [r for r in rows if r["hit"] is not None]
        conf = {"high": 0, "mid": 0, "low": 0, None: 0}
        for r in runs:
            conf[r["confidence"]] = conf.get(r["confidence"], 0) + 1
        lines.append(
            f"| {name} | {len(rows)} | {int(statistics.median(firsts))} | {int(statistics.median(totals))} "
            f"| {statistics.mean(r['n_sources'] for r in runs):.1f} "
            f"| {sum(1 for r in hits if r['hit'])}/{len(hits)} | {conf.get('high',0)}/{conf.get('mid',0)}/{conf.get('low',0)} |"
        )
    lines += ["", "## 逐组明细", ""]
    for name, rows in results.items():
        lines.append(f"### {name}")
        lines.append("")
        lines.append("| 类型 | 问题 | 首token(ms) | 总(ms) | 来源 | 置信度 | 要点 |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in rows:
            hit = "" if r["hit"] is None else ("Y" if r["hit"] else "N")
            lines.append(
                f"| {r['q']['type']} | {r['q']['question'][:28]} | {r['run']['first_ms'] and int(r['run']['first_ms'])} "
                f"| {int(r['run']['total_ms'])} | {r['run']['n_sources']} | {r['run']['confidence']} | {hit} |"
            )
        lines.append("")

    out = ROOT / "docs" / "evidence" / f"runtime-matrix-{time.strftime('%Y%m%d-%H%M')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"证据文件: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
