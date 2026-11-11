"""现场演示三场景运行器。

用法：
  uv run --project server python demo/run_demo.py --base-url http://127.0.0.1:8010

脚本会：
  1. 检查后端健康状态与知识库；
  2. 如缺少评测夹具，自动入库 data/corpus/fixture_术语混写_模拟维保手册.md；
  3. 依次运行场景 1/2/3，并打印回答、来源、置信度与自动检查结果；
  4. 场景 3 通过会话附件上传 120 页演示 PDF、润滑点位图和保养周期表。

注意：脚本只读取本地物料并调用本机 API，不包含任何密钥。
"""

import argparse
import json
import pathlib
import sys
import time
from typing import Any

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data" / "corpus" / "fixture_术语混写_模拟维保手册.md"
SCENARIOS = {
    1: ROOT / "demo" / "scenario1" / "questions.json",
    2: ROOT / "demo" / "scenario2" / "questions.json",
    3: ROOT / "demo" / "scenario3" / "questions.json",
}


def _event_stream(lines):
    event = None
    data_lines: list[str] = []
    for line in lines:
        if line == "":
            if event and data_lines:
                yield event, json.loads("\n".join(data_lines))
            event = None
            data_lines = []
            continue
        if line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: "):
            data_lines.append(line[6:])
    if event and data_lines:
        yield event, json.loads("\n".join(data_lines))


def chat(
    client: httpx.Client,
    base_url: str,
    session_id: str,
    question: str,
    attachment_ids: list[str] | None = None,
) -> dict[str, Any]:
    payload = {"message": question, "mode": "kb", "sessionId": session_id}
    if attachment_ids:
        payload["attachmentIds"] = attachment_ids
    tokens: list[str] = []
    sources: list[dict] = []
    trace: list[dict] = []
    done: dict | None = None
    error: dict | None = None
    with client.stream("POST", f"{base_url}/api/chat", json=payload) as response:
        if response.status_code != 200:
            response.read()
            raise RuntimeError(f"chat HTTP {response.status_code}: {response.text[:300]}")
        for event, data in _event_stream(response.iter_lines()):
            if event == "token":
                tokens.append(str(data.get("delta", "")))
            elif event == "sources":
                sources = data.get("items") or []
            elif event == "trace":
                trace.append(data)
            elif event == "done":
                done = data
            elif event == "error":
                error = data
    return {
        "answer": "".join(tokens),
        "sources": sources,
        "trace": trace,
        "done": done,
        "error": error,
    }


def create_session(client: httpx.Client, base_url: str, title: str) -> str:
    response = client.post(f"{base_url}/api/sessions", json={"title": title})
    response.raise_for_status()
    return response.json()["data"]["id"]


def upload_attachment(
    client: httpx.Client, base_url: str, session_id: str, path: pathlib.Path
) -> dict:
    data = path.read_bytes()
    response = client.post(
        f"{base_url}/api/uploads/init",
        json={
            "sessionId": session_id,
            "filename": path.name,
            "size": len(data),
            "mime": "application/octet-stream",
        },
    )
    response.raise_for_status()
    init = response.json()["data"]
    total = int(init["totalChunks"])
    chunk_size = int(init["chunkSize"])
    print(f"    上传 {path.name}: {len(data) / (1024 * 1024):.1f}MB / {total} 片")
    for index in range(total):
        chunk = data[index * chunk_size : (index + 1) * chunk_size]
        part = client.put(
            f"{base_url}/api/uploads/{init['uploadId']}/chunks/{index}?sessionId={session_id}",
            content=chunk,
            headers={"Content-Type": "application/octet-stream"},
        )
        part.raise_for_status()
    response = client.post(
        f"{base_url}/api/uploads/{init['uploadId']}/complete",
        json={"sessionId": session_id},
    )
    response.raise_for_status()
    return response.json()["data"]


def ensure_fixture(client: httpx.Client, base_url: str, auto_ingest: bool) -> bool:
    response = client.get(f"{base_url}/api/kb/documents")
    response.raise_for_status()
    documents = response.json().get("data") or []
    present = any("fixture_术语混写" in (item.get("filename") or "") for item in documents)
    if present:
        print("知识库：已存在场景 1/2 所需评测夹具")
        return True
    if not auto_ingest:
        print("知识库：缺少 fixture_术语混写_模拟维保手册.md，请先入库该文件")
        return False
    if not FIXTURE.exists():
        print(f"知识库：夹具文件不存在：{FIXTURE}")
        return False
    print("知识库：缺少评测夹具，正在自动入库 ...")
    with FIXTURE.open("rb") as handle:
        response = client.post(
            f"{base_url}/api/kb/ingest",
            files={"file": (FIXTURE.name, handle, "text/markdown")},
            data={"folder": "演示语料"},
        )
    if response.status_code != 200:
        print(f"知识库：自动入库失败 HTTP {response.status_code}: {response.text[:300]}")
        return False
    print(f"知识库：入库完成 {response.json().get('data')}")
    return True


def check_scene(config: dict, result: dict) -> tuple[bool, list[str]]:
    failures: list[str] = []
    if result.get("error"):
        failures.append(f"SSE error: {result['error']}")
    if not result.get("done"):
        failures.append("未收到 done 事件")
    answer = result.get("answer") or ""
    for item in config.get("mustContain", []):
        if item.lower() not in answer.lower():
            failures.append(f"回答缺少必需内容：{item}")
    for group in config.get("mustContainAny", []):
        if not any(token.lower() in answer.lower() for token in group):
            failures.append(f"回答缺少任一必需内容：{'/'.join(group)}")

    sources = result.get("sources") or []
    if len(sources) < int(config.get("minSources", 1)):
        failures.append(f"来源数量不足：{len(sources)}")
    expected_docs = config.get("sourceDocContains") or []
    if expected_docs and not any(
        any(key.lower() in (source.get("docName") or "").lower() for key in expected_docs)
        for source in sources
    ):
        failures.append(f"来源未命中：{expected_docs}")
    if config.get("requireAttachmentSource") and not any(
        source.get("kind") == "attachment" for source in sources
    ):
        failures.append("来源中缺少 kind=attachment 的会话附件")
    return not failures, failures


def print_result(config: dict, result: dict, passed: bool, failures: list[str]) -> None:
    print("-" * 72)
    print(f"[场景 {config['scenario']}] {config['title']}")
    print(f"问题：{config['question']}")
    answer = (result.get("answer") or "").strip().replace("\n", " ")
    print(f"回答：{answer[:600]}{'…' if len(answer) > 600 else ''}")
    print("来源：")
    for source in (result.get("sources") or [])[:5]:
        page = f" · 第{source['page']}页" if source.get("page") else ""
        kind = "会话附件" if source.get("kind") == "attachment" else "知识库"
        print(f"  [{source.get('index')}] {kind} · {source.get('docName')} · {source.get('section')}{page}")
    done = result.get("done") or {}
    print(f"置信度：{done.get('confidence')}（{done.get('confidence_reason', '')}）")
    if failures:
        print(f"自动检查：FAIL — {'；'.join(failures)}")
    else:
        print("自动检查：PASS")
    should = config.get("shouldContain") or []
    if should:
        missing = [item for item in should if item.lower() not in (result.get("answer") or "").lower()]
        if missing:
            print(f"提示：回答未包含加分要点 {missing}，请人工确认")


def load_config(scenario: int) -> dict:
    path = SCENARIOS[scenario]
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="IndocQA 三场景现场演示运行器")
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument(
        "--scenario",
        choices=["1", "2", "3", "all"],
        default="all",
        help="只运行指定场景，默认全部",
    )
    parser.add_argument(
        "--no-auto-ingest",
        action="store_true",
        help="缺少评测夹具时不自动入库",
    )
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    with httpx.Client(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
        print("=" * 72)
        print("IndocQA 三场景演示")
        print("=" * 72)
        health = client.get(f"{base_url}/api/health")
        health.raise_for_status()
        info = health.json()
        print(
            f"后端：{info.get('status')} · 知识库 {info.get('kb', {}).get('documents')} 份 / "
            f"{info.get('kb', {}).get('chunks')} 块 · 嵌入 {info.get('embedding', {}).get('backend')}"
        )
        if not ensure_fixture(client, base_url, not args.no_auto_ingest):
            return 1

        scenarios = [1, 2, 3] if args.scenario == "all" else [int(args.scenario)]
        results: list[tuple[int, bool]] = []
        for scenario in scenarios:
            config = load_config(scenario)
            session_id = create_session(client, base_url, config["title"])
            print(f"\n[场景 {scenario}] 会话 {session_id}")
            attachment_ids: list[str] = []
            if config.get("attachments"):
                for relative in config["attachments"]:
                    path = ROOT / relative
                    if not path.exists():
                        print(f"    缺少演示物料：{path}")
                        results.append((scenario, False))
                        continue
                    attachment = upload_attachment(client, base_url, session_id, path)
                    attachment_ids.append(attachment["id"])
                    print(
                        f"    完成 {attachment['filename']} · {attachment['kind']} · "
                        f"{attachment.get('parseStatus')} · {attachment.get('meta')}"
                    )
            started = time.perf_counter()
            result = chat(
                client,
                base_url,
                session_id,
                config["question"],
                attachment_ids or None,
            )
            elapsed = time.perf_counter() - started
            passed, failures = check_scene(config, result)
            print(f"耗时：{elapsed:.1f}s")
            print_result(config, result, passed, failures)
            results.append((scenario, passed))

        print("\n" + "=" * 72)
        passed_count = sum(1 for _, passed in results if passed)
        print(f"汇总：{passed_count}/{len(results)} 通过")
        for scenario, passed in results:
            print(f"  场景 {scenario}: {'PASS' if passed else 'FAIL'}")
        print("会话已保留，可在前端侧栏按标题打开查看引用、执行链与附件预览。")
        return 0 if passed_count == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
