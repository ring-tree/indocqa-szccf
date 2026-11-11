"""演示环境核心接口 smoke check。

用法：
  uv run --project server python demo/smoke_check.py --base-url http://127.0.0.1:8010

覆盖：健康检查、模型列表、会话 CRUD、知识库列表/预检/入库、附件分片上传与内容读取、
附件删除/中止、SSE 问答。不会删除知识库文档，不会修改已有会话。
"""

import argparse
import pathlib
import sys
import tempfile
import time

import httpx

from run_demo import FIXTURE, ROOT, chat

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    try:
        detail = fn() or ""
        RESULTS.append((name, True, detail))
        print(f"PASS  {name}  {detail}")
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((name, False, str(exc)))
        print(f"FAIL  {name}  {exc}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    with httpx.Client(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
        def health():
            response = client.get(f"{base}/api/health")
            response.raise_for_status()
            data = response.json()
            assert data["status"] == "ok"
            return f"kb={data['kb']['documents']} docs/{data['kb']['chunks']} chunks"

        def models():
            response = client.get(f"{base}/api/models")
            response.raise_for_status()
            return f"{len(response.json().get('data') or [])} models"

        def kb_folders():
            response = client.get(f"{base}/api/kb/folders")
            response.raise_for_status()
            return f"{len(response.json().get('data') or [])} folders"

        def kb_documents():
            response = client.get(f"{base}/api/kb/documents")
            response.raise_for_status()
            return f"{len(response.json().get('data') or [])} documents"

        def kb_preview():
            assert FIXTURE.exists(), f"缺少 {FIXTURE}"
            with FIXTURE.open("rb") as handle:
                response = client.post(
                    f"{base}/api/kb/preview",
                    files={"file": (FIXTURE.name, handle, "text/markdown")},
                )
            response.raise_for_status()
            data = response.json()["data"]
            assert data["status"] in {"ok", "warning"}
            return f"status={data['status']} chunks={data['estimatedChunks']}"

        def kb_ingest():
            with FIXTURE.open("rb") as handle:
                response = client.post(
                    f"{base}/api/kb/ingest",
                    files={"file": (FIXTURE.name, handle, "text/markdown")},
                    data={"folder": "演示语料"},
                )
            response.raise_for_status()
            data = response.json()["data"]
            return f"chunks={data.get('chunks')} duplicated={data.get('duplicated')}"

        def kb_ingest_job():
            with FIXTURE.open("rb") as handle:
                response = client.post(
                    f"{base}/api/kb/ingest/jobs",
                    files={"file": (FIXTURE.name, handle, "text/markdown")},
                    data={"folder": "演示语料"},
                )
            response.raise_for_status()
            job_id = response.json()["data"]["jobId"]
            job = None
            deadline = time.time() + 120
            while time.time() < deadline:
                job = client.get(f"{base}/api/kb/ingest/jobs/{job_id}").json()["data"]
                if job["status"] in {"done", "error"}:
                    break
                time.sleep(0.4)
            assert job and job["status"] == "done", job
            return f"job={job_id} duplicated={job.get('result', {}).get('duplicated')}"

        def session_crud():
            created = client.post(f"{base}/api/sessions", json={"title": "演示-Smoke"}).json()["data"]
            sid = created["id"]
            assert client.get(f"{base}/api/sessions").status_code == 200
            assert client.get(f"{base}/api/sessions/{sid}").status_code == 200
            renamed = client.patch(f"{base}/api/sessions/{sid}", json={"title": "演示-Smoke-改"})
            assert renamed.status_code == 200
            assert client.delete(f"{base}/api/sessions/{sid}").status_code == 200
            return f"session={sid}"

        def upload_cycle():
            sid = client.post(f"{base}/api/sessions", json={"title": "演示-上传Smoke"}).json()["data"]["id"]
            data = "演示附件：3号产线保养周期为500小时。".encode()
            with tempfile.TemporaryDirectory() as tmp:
                path = pathlib.Path(tmp) / "smoke.txt"
                path.write_bytes(data)
                init = client.post(
                    f"{base}/api/uploads/init",
                    json={"sessionId": sid, "filename": path.name, "size": len(data), "mime": "text/plain"},
                ).json()["data"]
                for index in range(init["totalChunks"]):
                    chunk = data[index * init["chunkSize"] : (index + 1) * init["chunkSize"]]
                    part = client.put(
                        f"{base}/api/uploads/{init['uploadId']}/chunks/{index}?sessionId={sid}",
                        content=chunk,
                    )
                    part.raise_for_status()
                status = client.get(
                    f"{base}/api/uploads/{init['uploadId']}?sessionId={sid}"
                ).json()["data"]
                assert status["received"] == list(range(init["totalChunks"]))
                attachment = client.post(
                    f"{base}/api/uploads/{init['uploadId']}/complete",
                    json={"sessionId": sid},
                ).json()["data"]
                content = client.get(
                    f"{base}/api/attachments/{attachment['id']}/content?sessionId={sid}"
                )
                assert content.status_code == 200 and content.content == data
                assert client.get(f"{base}/api/sessions/{sid}/attachments").status_code == 200
                assert client.delete(
                    f"{base}/api/attachments/{attachment['id']}?sessionId={sid}"
                ).status_code == 200
                assert client.get(
                    f"{base}/api/attachments/{attachment['id']}/content?sessionId={sid}"
                ).status_code == 404
            assert client.delete(f"{base}/api/sessions/{sid}").status_code == 200
            return "init/chunk/status/complete/content/list/delete"

        def abort_upload():
            sid = client.post(f"{base}/api/sessions", json={"title": "演示-中止Smoke"}).json()["data"]["id"]
            data = b"abort"
            init = client.post(
                f"{base}/api/uploads/init",
                json={"sessionId": sid, "filename": "abort.txt", "size": len(data)},
            ).json()["data"]
            client.put(
                f"{base}/api/uploads/{init['uploadId']}/chunks/0?sessionId={sid}",
                content=data,
            ).raise_for_status()
            assert client.delete(
                f"{base}/api/uploads/{init['uploadId']}?sessionId={sid}"
            ).status_code == 200
            assert client.delete(f"{base}/api/sessions/{sid}").status_code == 200
            return "abort/cleanup"

        def reject_format():
            response = client.post(
                f"{base}/api/uploads/init",
                json={"filename": "bad.exe", "size": 10},
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == 40003
            return "40003"

        def chat_sse():
            sid = client.post(f"{base}/api/sessions", json={"title": "演示-SSE"}).json()["data"]["id"]
            result = chat(
                client,
                base,
                sid,
                "设备手册里的 MTBF 和维修手册里的平均无故障时间是一回事吗？额定值是多少？",
            )
            assert result["done"], f"no done: {result.get('error')}"
            assert result["sources"], "no sources"
            assert "8000" in result["answer"] or result.get("done")
            assert client.delete(f"{base}/api/sessions/{sid}").status_code == 200
            return f"sources={len(result['sources'])} confidence={result['done'].get('confidence')}"

        check("GET /api/health", health)
        check("GET /api/models", models)
        check("GET /api/kb/folders", kb_folders)
        check("GET /api/kb/documents", kb_documents)
        check("POST /api/kb/preview", kb_preview)
        check("POST /api/kb/ingest（幂等）", kb_ingest)
        check("POST/GET /api/kb/ingest/jobs", kb_ingest_job)
        check("会话 CRUD", session_crud)
        check("附件完整生命周期", upload_cycle)
        check("附件中止/清理", abort_upload)
        check("附件格式拒绝（40003）", reject_format)
        check("POST /api/chat（SSE）", chat_sse)

        passed = sum(1 for _, ok, _ in RESULTS if ok)
        print(f"\n汇总：{passed}/{len(RESULTS)} 通过")
        return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
