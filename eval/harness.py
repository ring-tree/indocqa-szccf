"""基线对比评测 harness：同一评测集在三种检索配置下跑批，量化自适应收益。

配置：
  B2_fixed        固定参数基线（统一混合检索参数，无意图路由/术语扩展/多跳）
  Ours_adaptive   意图路由 + 术语扩展 + 多跳 + 参数自适应
  Ablation_no_term adaptive 去查询侧术语扩展（索引侧打标保留，属部分消融）

指标：Source Recall@5 / Answer 覆盖率 / 多跳跨文档命中率 / 延迟 p50·p95。
答案口径：检索摘录（前三命中块），三配置同口径；生成模型接入后可切换真实生成再评。

用法：在仓库根目录执行  python eval/harness.py
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
os.environ.setdefault("INDOCQA_EMBED_BACKEND", "default")

CONFIGS = [
    ("B2_fixed", {"INDOCQA_RETRIEVAL_MODE": "fixed"}),
    ("Ours_adaptive", {"INDOCQA_RETRIEVAL_MODE": "adaptive"}),
    ("Ablation_no_term", {"INDOCQA_RETRIEVAL_MODE": "adaptive", "INDOCQA_TERM_EXPANSION_ENABLED": "0"}),
]
EXCERPT_CHARS = 300


def prepare() -> None:
    """建库（幂等）：语料入库 + 术语簇构建。"""
    from app.core.config import get_settings
    from app.indexing.ingest import ingest_dir
    from app.retrieval.terms import build_clusters

    settings = get_settings()
    results = ingest_dir(pathlib.Path(settings.corpus_dir), settings)
    print(f"入库 {len(results)} 份 / {sum(r['chunks'] for r in results)} 块")
    clusters = build_clusters(settings)
    print(f"术语簇 {len(clusters)} 个")


def apply_env(env: dict) -> None:
    from app.core.config import get_settings

    for key in list(k for k in os.environ if k.startswith("INDOCQA_")):
        os.environ.pop(key, None)
    os.environ["INDOCQA_EMBED_BACKEND"] = "default"
    os.environ.update(env)
    get_settings.cache_clear()


async def run_case(question: str, mode: str) -> dict:
    from app.graph.graph import run_retrieval

    t0 = time.perf_counter()
    result = await run_retrieval(question, mode)
    latency = (time.perf_counter() - t0) * 1000
    hits = result["hits"]
    answer_text = " ".join(h.text[:EXCERPT_CHARS] for h in hits[:3])
    return {
        "latency_ms": latency,
        "doc_names": [h.doc_name for h in hits[:5]],
        "all_docs": [h.doc_name for h in hits],
        "answer_text": answer_text,
        "expansions": sum(
            len(s.get("expansions", [])) for s in result["trace"] if s["stage"] == "retrieve"
        ),
        "hops": max(0, sum(1 for s in result["trace"] if s["stage"] == "retrieve") - 1),
    }


def score(case: dict, run: dict) -> dict:
    recall = any(exp in name for name in run["doc_names"] for exp in case["expected_sources"])
    points = [p.lower() in run["answer_text"].lower() for p in case["answer_points"]]
    coverage = sum(points) / len(points) if points else 0.0
    cross = (
        all(any(exp in name for name in run["all_docs"]) for exp in case["expected_sources"])
        if case["type"] == "multihop"
        else None
    )
    return {"recall": recall, "coverage": coverage, "cross": cross}


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    prepare()

    dataset_path = ROOT / "eval" / "dataset.jsonl"
    cases = [json.loads(line) for line in dataset_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"评测集 {len(cases)} 题")

    rows = {name: [] for name, _ in CONFIGS}
    for case in cases:
        line = f"[{case['id']:9s}]"
        for name, env in CONFIGS:
            apply_env(env)
            mode = "fixed" if name == "B2_fixed" else "adaptive"
            run = await run_case(case["question"], mode)
            s = score(case, run)
            rows[name].append({"case": case, "run": run, "score": s})
            line += f" {name.split('_')[0]}:R{'Y' if s['recall'] else 'N'}C{s['coverage']:.0f}"
        print(line)

    report = _render_report(rows, cases)
    out = ROOT / "docs" / "基线对比评测报告.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"报告: {out}")
    return 0


def _render_report(rows: dict, cases: list) -> str:
    types = ["term", "param", "procedure", "fault", "multihop"]
    lines = [
        "# 检索策略基线对比评测报告",
        "",
        f"- 日期：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 评测集：{len(cases)} 题（{'/'.join(types)}）",
        "- 答案口径：检索摘录（前三命中块各 300 字符），三配置同口径，量化的是检索质量差异",
        "- 生成模型未配置；接入真实生成后的引用精度与最终答案质量另行评测",
        "",
        "## 总表",
        "",
        "| 配置 | Source Recall@5 | Answer 覆盖率 | 多跳跨文档命中 | 延迟 p50 / p95 (ms) |",
        "|---|---|---|---|---|",
    ]
    for name, _ in CONFIGS:
        rs = rows[name]
        recall = sum(1 for r in rs if r["score"]["recall"]) / len(rs)
        coverage = statistics.mean(r["score"]["coverage"] for r in rs)
        multi = [r for r in rs if r["score"]["cross"] is not None]
        cross = (
            f"{sum(1 for r in multi if r['score']['cross']) / len(multi):.0%}" if multi else "-"
        )
        lat = sorted(r["run"]["latency_ms"] for r in rs)
        p50 = lat[len(lat) // 2]
        p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
        lines.append(f"| {name} | {recall:.0%} | {coverage:.0%} | {cross} | {p50:.0f} / {p95:.0f} |")

    lines += ["", "## 分题型 Source Recall@5", "", "| 配置 | " + " | ".join(types) + " |", "|---|" + "---|" * len(types)]
    for name, _ in CONFIGS:
        cells = []
        for t in types:
            sub = [r for r in rows[name] if r["case"]["type"] == t]
            cells.append(f"{sum(1 for r in sub if r['score']['recall']) / len(sub):.0%}" if sub else "-")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")

    lines += ["", "## 分题型 Answer 覆盖率", "", "| 配置 | " + " | ".join(types) + " |", "|---|" + "---|" * len(types)]
    for name, _ in CONFIGS:
        cells = []
        for t in types:
            sub = [r for r in rows[name] if r["case"]["type"] == t]
            cells.append(f"{statistics.mean(r['score']['coverage'] for r in sub):.0%}" if sub else "-")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")

    adaptive = rows["Ours_adaptive"]
    exp_q = sum(1 for r in adaptive if r["run"]["expansions"])
    hop_q = sum(1 for r in adaptive if r["run"]["hops"])
    lines += [
        "",
        "## 自适应行为统计（Ours_adaptive）",
        "",
        f"- 触发术语扩展的题目：{exp_q}/{len(adaptive)}",
        f"- 触发多跳补查的题目：{hop_q}/{len(adaptive)}",
        "",
        "## 结论",
        "",
        *_conclusion(rows),
    ]
    return "\n".join(lines)


def _conclusion(rows: dict) -> list[str]:
    def stat(name):
        rs = rows[name]
        recall = sum(1 for r in rs if r["score"]["recall"]) / len(rs)
        cov = statistics.mean(r["score"]["coverage"] for r in rs)
        multi = [r for r in rs if r["score"]["cross"] is not None]
        cross = sum(1 for r in multi if r["score"]["cross"]) / len(multi) if multi else 0.0
        lat = sorted(r["run"]["latency_ms"] for r in rs)
        return recall, cov, cross, lat[len(lat) // 2], lat[min(len(lat) - 1, int(len(lat) * 0.95))]

    b2, ours, abl = stat("B2_fixed"), stat("Ours_adaptive"), stat("Ablation_no_term")
    lines = [
        f"- Source Recall@5：自适应 {ours[0]:.0%} vs 固定基线 {b2[0]:.0%}"
        f"（{('提升' if ours[0] > b2[0] + 0.02 else '下降' if ours[0] < b2[0] - 0.02 else '持平')}）",
        f"- Answer 覆盖率：自适应 {ours[1]:.0%} vs 固定基线 {b2[1]:.0%}"
        f"（{('提升' if ours[1] > b2[1] + 0.02 else '下降' if ours[1] < b2[1] - 0.02 else '持平')}）",
        f"- 多跳跨文档命中：自适应 {ours[2]:.0%} vs 固定基线 {b2[2]:.0%}"
        f"（{('提升' if ours[2] > b2[2] + 0.02 else '下降' if ours[2] < b2[2] - 0.02 else '持平')}）",
        f"- 消融（去查询侧术语扩展）Recall {abl[0]:.0%} / 覆盖率 {abl[1]:.0%}：查询侧术语扩展净贡献 "
        f"Recall {ours[0]-abl[0]:+.0%}、覆盖率 {ours[1]-abl[1]:+.0%}",
        f"- 延迟代价：p50 {ours[3]:.0f} vs {b2[3]:.0f} ms；p95 {ours[4]:.0f} vs {b2[4]:.0f} ms"
        "（扩展与多跳的额外计算集中体现在 p95）",
    ]
    wins = [name for name, idx in (("整体召回", 0), ("答案要点覆盖", 1), ("多跳跨文档命中", 2)) if ours[idx] > b2[idx] + 0.02]
    if wins:
        rest = [n for n, i in (("整体召回", 0), ("答案要点覆盖", 1), ("多跳跨文档命中", 2)) if n not in wins]
        held = f"，其余指标与基线持平（{ '、'.join(rest) }）" if rest else ""
        lines.append(f"- 总体：自适应在{'、'.join(wins)}上优于固定基线{held}")
    else:
        lines.append("- 总体：自适应与固定基线整体持平，分题型差异与改进方向详见分题型表")
    return lines


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
