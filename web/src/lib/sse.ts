export interface SSEEvent {
  event: string;
  data: unknown;
}

/** 解析 POST 返回的 text/event-stream，逐事件回调。 */
export async function postSSE(
  url: string,
  body: unknown,
  onEvent: (ev: SSEEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!resp.ok || !resp.body) {
    let message = `HTTP ${resp.status}`;
    try {
      const parsed = await resp.json();
      message = parsed?.error?.message ?? message;
    } catch {
      /* 非 JSON 错误体，保留 HTTP 状态码提示 */
    }
    onEvent({ event: "error", data: { message } });
    return;
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep = buffer.indexOf("\n\n");
    while (sep >= 0) {
      const block = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      handleBlock(block, onEvent);
      sep = buffer.indexOf("\n\n");
    }
  }
  if (buffer.trim()) handleBlock(buffer, onEvent);
}

function handleBlock(block: string, onEvent: (ev: SSEEvent) => void): void {
  let event = "message";
  let dataRaw = "";
  for (const line of block.split("\n")) {
    if (line.startsWith("event: ")) event = line.slice(7).trim();
    else if (line.startsWith("data: ")) dataRaw += line.slice(6);
  }
  if (!dataRaw) return;
  try {
    onEvent({ event, data: JSON.parse(dataRaw) });
  } catch {
    /* 跳过无法解析的块，保持流不中断 */
  }
}
