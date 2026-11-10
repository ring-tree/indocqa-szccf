"""生成典型场景的上下文组装实跑页面，并用 headless Chrome 截图。

用法：在 server/ 目录执行 `uv run python ../eval/context_scenarios.py`
输出：docs/context/scenario-*.html 与 scenario-*.png
"""

import asyncio
import html
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "context"
OUT.mkdir(parents=True, exist_ok=True)
CHROME = pathlib.Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")

SCENARIOS = [
    ("01-fault", "故障诊断", "空压机报错 E-05 应该怎么处理？", ["评测夹具"]),
    ("02-param", "参数查询", "LUB 更换的保养周期是多少小时？", ["评测夹具"]),
    ("03-term", "术语解释", "MTBF 和平均无故障时间有什么区别？", ["评测夹具"]),
    ("04-multihop", "跨文档多跳", "MTBF 和储气罐的作用分别是什么？", None),
    ("05-procedure", "操作步骤", "排气温度过高时应该采取哪些步骤？", ["评测夹具"]),
    ("06-boundary", "库外问题", "量子计算机的工作原理是什么？", None),
]


def esc(value) -> str:
    return html.escape(str(value or ""))


async def run(question: str, folders: list[str] | None = None):
    from app.core.config import get_settings
    from app.graph import pipeline
    from app.session import store as session_store

    events = []
    async for event in pipeline.chat_stream(question, "kb", folders=folders):
        events.append(event)
    done = next((e["data"] for e in events if e["event"] == "done"), None)
    if done and done.get("sessionId"):
        rows = session_store.get_messages(get_settings().session_db, done["sessionId"])
        message = next((row for row in rows if row["id"] == done.get("messageId")), None)
        if message:
            done["contextManifest"] = message.get("contextManifest")
    return events


def render(name: str, title: str, question: str, events: list[dict]) -> str:
    context = next((e["data"] for e in events if e["event"] == "context"), {})
    sources = next((e["data"]["items"] for e in events if e["event"] == "sources"), [])
    trace = [e["data"] for e in events if e["event"] == "trace"]
    answer = "".join(e["data"]["delta"] for e in events if e["event"] == "token")
    done = next((e["data"] for e in events if e["event"] == "done"), {})
    manifest = done.get("contextManifest") or {}
    stats = context or {}
    cards = [
        ("意图", next((t.get("intent") for t in trace if t.get("stage") == "classify"), "-")),
        ("预算 token", stats.get("budget", "-")),
        ("实际使用", stats.get("used", "-")),
        ("利用率", f"{stats.get('utilization', 0):.1%}" if stats else "-"),
        ("证据单元", stats.get("layers", {}).get("evidence", "-")),
        ("去重", stats.get("duplicate", "-")),
        ("噪声过滤", stats.get("noisy", "-")),
        ("组装耗时", f"{stats.get('elapsedMs', 0):.2f} ms" if stats else "-"),
    ]
    cards_html = "".join(
        f'<div class="card"><span>{esc(key)}</span><strong>{esc(value)}</strong></div>'
        for key, value in cards
    )
    rows = "".join(
        f"<tr><td>[{item['index']}]</td><td>{esc(item['docName'])}<br><small>{esc(item['section'])}"
        f" · 第 {esc(item['page'] or '-')} 页</small></td><td>{esc(item['score'])}</td>"
        f"<td>{esc(item['snippet'][:180])}…</td></tr>"
        for item in sources
    )
    trace_html = "".join(
        f"<li><b>{esc(step.get('stage'))}</b> {esc({k: v for k, v in step.items() if k != 'stage'})}</li>"
        for step in trace
    )
    template_html = ""
    preview = ""
    if manifest:
        template_html = (
            f"<p class='muted'>清单 ID：{esc(manifest.get('traceId'))} · "
            f"模板 {esc(manifest.get('templateId'))}@{esc(manifest.get('templateVersion'))} · "
            f"引用映射 {len(manifest.get('citationMap', {}))} 项 · 完整性 "
            f"{'通过' if manifest.get('integrity') else '异常'}</p>"
            f"<p class='muted'>单元元数据字段：unitId / sourceType / sourceId / sessionId / "
            f"timestamp / chunkId / docId / docName / section / page → 可反向溯源</p>"
        )
        from app.context.templates import KB_V1

        intent = next((t.get("intent") for t in trace if t.get("stage") == "classify"), "general")
        rules = "\n".join(
            f"{index}) {rule.text}"
            for index, rule in enumerate(KB_V1.active_rules(intent), start=1)
        )
        evidence_text = "\n\n".join(
            f"[{item['index']}] {item['docName']} · {item['section']}\n{item['snippet']}"
            for item in sources
        )
        preview = (
            f"你是企业知识库问答助手。规则：\n{rules}\n\n"
            f"【资料】\n{evidence_text}\n\n【问题】\n{question}"
        )
    body = f"""
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<style>
* {{ box-sizing: border-box; }}
body {{ margin:0; background:#f3f5f9; color:#1f2937;
       font-family:"Microsoft YaHei","PingFang SC",Arial,sans-serif; }}
.wrap {{ width:1360px; margin:0 auto; padding:36px 40px 50px; }}
.hero {{ background:linear-gradient(135deg,#0f172a,#1e3a8a); color:#fff;
         border-radius:18px; padding:26px 30px; }}
.hero .tag {{ font-size:12px; letter-spacing:2px; color:#93c5fd; }}
.hero h1 {{ margin:8px 0 10px; font-size:28px; }}
.hero p {{ margin:0; font-size:16px; color:#dbeafe; }}
.cards {{ display:grid; grid-template-columns:repeat(8,1fr); gap:12px; margin:20px 0; }}
.card {{ background:#fff; border-radius:12px; padding:14px 16px; border:1px solid #e5e7eb;
         box-shadow:0 2px 8px rgba(15,23,42,.04); }}
.card span {{ display:block; font-size:12px; color:#6b7280; }}
.card strong {{ display:block; margin-top:6px; font-size:19px; color:#1d4ed8; }}
.panel {{ background:#fff; border-radius:14px; padding:20px 22px; margin-bottom:18px;
          border:1px solid #e5e7eb; }}
.panel h2 {{ margin:0 0 12px; font-size:17px; color:#0f172a; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th {{ text-align:left; background:#f8fafc; color:#475569; font-weight:600; padding:8px; }}
td {{ border-top:1px solid #eef2f7; padding:8px; vertical-align:top; line-height:1.55; }}
small {{ color:#64748b; }}
.muted {{ color:#64748b; font-size:12.5px; line-height:1.7; margin:6px 0; }}
pre {{ white-space:pre-wrap; word-break:break-word; background:#0f172a; color:#dbeafe;
       border-radius:10px; padding:14px; font-size:12px; line-height:1.6; max-height:260px;
       overflow:hidden; }}
.answer {{ font-size:14px; line-height:1.8; }}
ul {{ margin:0; padding-left:20px; font-size:13px; line-height:1.8; }}
.note {{ font-size:12px; color:#64748b; margin-top:10px; }}
</style></head><body><div class="wrap">
<div class="hero"><div class="tag">INDOCQA · CONTEXT ASSEMBLY · {esc(name)}</div>
<h1>{esc(title)}</h1><p>问题：{esc(question)}</p></div>
<div class="cards">{cards_html}</div>
<div class="panel"><h2>检索与组装执行链</h2><ul>{trace_html}</ul></div>
<div class="panel"><h2>引用来源（与回答 [n] 一一对应）</h2>
<table><thead><tr><th>编号</th><th>文档 / 章节</th><th>相似度</th><th>片段</th></tr></thead>
<tbody>{rows or '<tr><td colspan="4">无有效命中，走库外回答路径</td></tr>'}</tbody></table></div>
<div class="panel"><h2>溯源清单</h2>{template_html or '<p class="muted">本次未进入知识库组装（库外问题）。</p>'}</div>
<div class="panel"><h2>回答输出</h2><div class="answer">{esc(answer[:900])}</div></div>
<div class="panel"><h2>约束模板与资料区（最终提示词节选）</h2>
<pre>{esc(preview)}</pre>
<p class="note">约束模板为版本化规则清单，渲染后逐条校验 R1–R6；资料区与系统规则物理隔离。</p></div>
</div></body></html>"""
    return body


def screenshot(name: str):
    source = OUT / f"scenario-{name}.html"
    target = OUT / f"scenario-{name}.png"
    if not CHROME.exists():
        return
    cmd = [
        str(CHROME),
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        "--window-size=1440,2000",
        f"--screenshot={target}",
        source.as_uri(),
    ]
    subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp())
    os.environ["INDOCQA_CHROMA_DIR"] = str(ROOT / "server" / "data" / "chroma")
    os.environ["INDOCQA_UPLOAD_DIR"] = str(tmp / "uploads")
    os.environ["INDOCQA_SESSION_DB"] = str(tmp / "sessions.db")
    os.environ["INDOCQA_TERMS_FILE"] = str(ROOT / "data" / "terms" / "terms.json")
    os.environ["INDOCQA_EMBED_BACKEND"] = "ollama"
    for key in (
        "INDOCQA_LLM_BASE_URL",
        "INDOCQA_LLM_API_KEY",
        "INDOCQA_LLM_MODEL",
        "INDOCQA_LLM_BACKUP_BASE_URL",
        "INDOCQA_LLM_BACKUP_API_KEY",
        "INDOCQA_LLM_BACKUP_MODEL",
    ):
        os.environ[key] = ""
    from app.core.config import get_settings
    from app.indexing import store

    get_settings.cache_clear()
    store._client.cache_clear()
    settings = get_settings()
    print(f"embed={settings.embed_backend} chroma={settings.chroma_dir}")
    for name, title, question, folders in SCENARIOS:
        started = time.perf_counter()
        events = asyncio.run(run(question, folders))
        page = render(name, title, question, events)
        path = OUT / f"scenario-{name}.html"
        path.write_text(page, encoding="utf-8")
        screenshot(name)
        print(f"{name}: {time.perf_counter() - started:.2f}s -> {path.name}")
    print("done")


if __name__ == "__main__":
    main()
