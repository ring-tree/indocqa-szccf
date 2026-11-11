"""生成层 UI 验收：Markdown/公式渲染、复制 Markdown、复制纯文本。

用法：后端 8000 与前端 5173 已启动后，在仓库根目录执行
`uv run --project server python eval/generation_ui_check.py`
"""

import asyncio
import base64
import json
import pathlib
import subprocess
import tempfile
import time
import urllib.request

import websockets

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "generation-ui.png"
CHROME = pathlib.Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
PORT = 9333
PROMPT = "请用 Markdown 输出：一个两列表格（项目/周期），一行 **重点** 说明，以及公式 $E=mc^2$。"
APP_URL = "http://127.0.0.1:5174"


def _get_json(url: str, method: str = "GET"):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _start_chrome(profile: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            str(CHROME),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={profile}",
            "--window-size=1440,1100",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def main() -> None:
    profile = tempfile.mkdtemp(prefix="indocqa-ui-")
    proc = await asyncio.to_thread(_start_chrome, profile)
    try:
        for _ in range(50):
            try:
                _get_json(f"http://127.0.0.1:{PORT}/json/version")
                break
            except (OSError, ValueError, urllib.error.URLError):
                await asyncio.sleep(0.2)
        target = _get_json(f"http://127.0.0.1:{PORT}/json/new?{APP_URL}", "PUT")
        ws = await websockets.connect(target["webSocketDebuggerUrl"], max_size=30_000_000)
        counter = {"id": 0}

        async def call(method: str, params: dict | None = None):
            counter["id"] += 1
            message_id = counter["id"]
            await ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
            while True:
                raw = json.loads(await ws.recv())
                if raw.get("id") == message_id:
                    if "error" in raw:
                        raise RuntimeError(raw["error"])
                    return raw.get("result", {})

        async def evaluate(expression: str):
            result = await call(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True, "awaitPromise": True},
            )
            return result.get("result", {}).get("value")

        async def wait_for(expression: str, timeout: float = 90.0):
            deadline = time.time() + timeout
            while time.time() < deadline:
                if await evaluate(expression):
                    return True
                await asyncio.sleep(0.5)
            return False

        await call("Page.enable")
        await call("Runtime.enable")
        await call(
            "Browser.grantPermissions",
            {"origin": APP_URL, "permissions": ["clipboardReadWrite"]},
        )
        await call("Page.navigate", {"url": APP_URL})
        assert await wait_for("document.readyState === 'complete'", 30), "页面未加载"
        assert await wait_for(
            "[...document.querySelectorAll('button')].some(b => b.textContent.trim() === '直接对话')",
            20,
        ), "未找到直接对话按钮"
        await evaluate(
            "[...document.querySelectorAll('button')].find(b => b.textContent.trim() === '直接对话').click()"
        )
        await asyncio.sleep(0.5)
        prompt_js = json.dumps(PROMPT, ensure_ascii=False)
        await evaluate(
            f"""
            (() => {{
              const ta = document.querySelector('textarea');
              const setter = Object.getOwnPropertyDescriptor(
                HTMLTextAreaElement.prototype, 'value'
              ).set;
              setter.call(ta, {prompt_js});
              ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
              return ta.value;
            }})()
            """
        )
        await asyncio.sleep(0.3)
        await evaluate("document.querySelector('button[aria-label=\"发送消息\"]').click()")
        if not await wait_for("!!document.querySelector('.ui-copy-btn')", 120):
            debug = await evaluate(
                """
                ({
                  body: (document.body.innerText || '').slice(0, 1200),
                  textarea: document.querySelector('textarea')?.value,
                  sendDisabled: document.querySelector('button[aria-label="发送消息"]')?.disabled,
                  messages: [...document.querySelectorAll('.ui-message-assistant')].map(
                    node => (node.innerText || '').slice(0, 300)
                  ),
                  error: document.querySelector('[role="alert"]')?.innerText,
                  copyButtons: document.querySelectorAll('.ui-copy-btn').length
                })
                """
            )
            print("DEBUG:", json.dumps(debug, ensure_ascii=False, indent=2))
            raise AssertionError("回答未完成")
        marks = await evaluate(
            """
            ({
              strong: !!document.querySelector('.markdown strong'),
              table: !!document.querySelector('.markdown table'),
              katex: !!document.querySelector('.markdown .katex'),
              copy: !!document.querySelector('.ui-copy-btn'),
              text: (document.querySelector('.ui-message-assistant')?.innerText || '').slice(0, 300)
            })
            """
        )

        await evaluate("document.querySelector('.ui-copy-btn').click()")
        await asyncio.sleep(0.2)
        await evaluate(
            "[...document.querySelectorAll('.ui-copy-menu button')].find(b => b.textContent.includes('复制为 Markdown')).click()"
        )
        assert await wait_for(
            "document.querySelector('.ui-copy-toast')?.textContent.includes('已复制 Markdown')", 5
        ), "Markdown 复制反馈缺失"
        markdown_clip = await evaluate("navigator.clipboard.readText()")
        shot = await call("Page.captureScreenshot", {"format": "png", "fromSurface": True})
        markdown_shot = ROOT / "docs" / "generation-ui-markdown.png"
        markdown_shot.write_bytes(base64.b64decode(shot["data"]))

        await evaluate("document.querySelector('.ui-copy-btn').click()")
        await asyncio.sleep(0.2)
        await evaluate(
            "[...document.querySelectorAll('.ui-copy-menu button')].find(b => b.textContent.includes('复制为 TXT')).click()"
        )
        assert await wait_for(
            "document.querySelector('.ui-copy-toast')?.textContent.includes('已复制纯文本')", 5
        ), "纯文本复制反馈缺失"
        plain_clip = await evaluate("navigator.clipboard.readText()")

        shot = await call("Page.captureScreenshot", {"format": "png", "fromSurface": True})
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_bytes(base64.b64decode(shot["data"]))

        print(
            json.dumps(
                {
                    "marks": marks,
                    "markdown_clip_prefix": (markdown_clip or "")[:160],
                    "plain_clip_prefix": (plain_clip or "")[:160],
                    "markdown_has_markup": any(
                        token in (markdown_clip or "") for token in ("**", "|", "$")
                    ),
                    "plain_has_markup": any(
                        token in (plain_clip or "") for token in ("**", "##", "|---")
                    ),
                    "markdown_screenshot": str(markdown_shot),
                    "screenshot": str(OUT),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        await ws.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    asyncio.run(main())
