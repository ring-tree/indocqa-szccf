# API 接口文档

工阅 IndocQA 后端基于 FastAPI，默认监听 `http://127.0.0.1:8010`。所有接口以 `/api` 为前缀，请求与响应均为 JSON（文件上传为 `multipart/form-data`，问答为 `text/event-stream`）。

- 在线调试：启动后端后访问 `http://127.0.0.1:8010/docs`（Swagger UI）
- OpenAPI 契约：`http://127.0.0.1:8010/openapi.json`
- 认证：本地部署默认不启用鉴权；如需公网访问，请在反向代理层增加认证与 HTTPS

## 目录

- [1. 通用约定](#1-通用约定)
- [2. 错误码](#2-错误码)
- [3. 健康检查与模型](#3-健康检查与模型)
- [4. 问答（SSE）](#4-问答sse)
- [5. 会话管理](#5-会话管理)
- [6. 知识库：入库与预检](#6-知识库入库与预检)
- [7. 知识库：目录树](#7-知识库目录树)
- [8. 知识库：文档与移动](#8-知识库文档与移动)
- [9. AI 分类](#9-ai-分类)
- [10. 调用示例](#10-调用示例)

---

## 1. 通用约定

### 1.1 成功响应

除 SSE 外，所有接口统一返回：

```json
{ "data": { } }
```

`data` 可能是对象、数组或布尔值。

### 1.2 失败响应

```json
{
  "error": {
    "code": 40001,
    "message": "请求参数不合法",
    "detail": "可选的补充信息"
  }
}
```

FastAPI/Pydantic 的请求体校验失败返回 HTTP 422，结构为默认的 `detail` 数组。

### 1.3 常用限制

| 项 | 限制 |
| --- | --- |
| 单条提问 | 0–2000 字符；`message` 与 `attachmentIds` 至少提供一个 |
| `topK` | 1–20 |
| 检索目录数 | 最多 20 个 |
| 知识库上传文件 | 单个 ≤ 20 MB；支持 `.pdf` / `.md` / `.markdown` / `.txt` |
| 会话附件 | 单个 ≤ 100 MB；单条消息 ≤ 10 个；4 MB 分片；支持图片、PDF/DOCX/MD/TXT/CSV/XLSX/PPTX、音频、视频 |
| 目录路径 | 单级 ≤ 40 字、层级 ≤ 6、总长 ≤ 200 字 |
| 会话标题 | ≤ 60 字符 |

---

## 2. 错误码

| code | HTTP | 含义 | 典型处理 |
| --- | ---: | --- | --- |
| 40001 | 400 | 参数校验失败 | 检查请求字段、目录路径、文件格式 |
| 40002 | 400 | 文件超过限制（知识库 20 MB / 会话附件 100 MB） | 压缩或拆分文档 |
| 40003 | 400 | 文件格式不支持，或文件内容与扩展名不符 | 转换为支持的格式 |
| 40004 | 400 | 单条消息附件超过 10 个 | 减少附件数量 |
| 40005 | 400 | 上传任务不存在、已过期或已完成 | 重新上传 |
| 40006 | 400 | 分片序号、大小或文件总大小不合法 | 重新上传缺失分片 |
| 40301 | 403 | 附件/会话无权访问 | 使用正确的 sessionId 或新建会话 |
| 40401 | 404 | 知识库为空 | 先在文档管理页入库，或仅上传会话附件 |
| 40402 | 404 | 文档/目录/入库任务不存在 | 刷新列表后重试 |
| 40403 | 404 | 会话不存在或已过期 | 新建会话 |
| 40404 | 404 | 附件不存在或已删除 | 重新上传 |
| 40901 | 409 | 上传状态冲突（重复完成/正在合并） | 稍后重试 |
| 40902 | 409 | 向量库与当前嵌入后端不一致 | 重建索引（见部署文档） |
| 40903 | 409 | 知识库正被另一个进程写入 | 等待写入结束后重试 |
| 40904 | 409 | 未配置输出层模型 | 配置 `INDOCQA_LLM_*` 后重启 |
| 42901 | 502 | 上游模型限流/不可用/流式超时；检索超时也复用该码 | 稍后重试，或切换模型 profile |
| 50001 | 500 | 检索服务异常（嵌入或向量库） | 检查 Ollama / 向量库状态 |
| 50002 | 500 | 未归类的服务端错误 | 查看后端日志 |
| 50003 | 500 | 附件存储/合并/清理失败 | 重试或更换文件 |

---

## 3. 健康检查与模型

### 3.1 健康检查

`GET /api/health`

响应：

```json
{
  "status": "ok",
  "version": "0.1.0",
  "kb": { "documents": 3, "chunks": 401, "state": "ready" },
  "llm": { "configured": true, "reachable": null },
  "embedding": { "mode": "auto", "backend": "ollama-bge-m3", "model": "bge-m3" }
}
```

| 字段 | 说明 |
| --- | --- |
| `kb.state` | `ready` / `empty` / `not_indexed` |
| `llm.configured` | 是否配置了主模型地址与 Key |
| `embedding.backend` | `ollama-*` / `fe-*` / `minilm` |

### 3.2 模型列表

`GET /api/models`

只返回已配置的模型 profile，不包含密钥与上游地址。

```json
{
  "data": [
    { "id": "primary", "model": "qwen3.8-27b" },
    { "id": "profile2", "model": "kimi-k3" }
  ]
}
```

---

## 4. 问答（SSE）

`POST /api/chat`

以 Server-Sent Events 流式返回。请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `message` | string | 否 | 用户问题，0–2000 字符；与 `attachmentIds` 至少提供一个 |
| `mode` | string | 是 | `kb` 知识库问答；`direct` 直接对话 |
| `sessionId` | string | 否 | 会话 ID；不传则自动新建 |
| `attachmentIds` | string[] | 否 | 会话附件 ID，最多 10 个；需先调用 `/api/uploads/*` 完成上传 |
| `profile` | string | 否 | 模型 profile ID；`auto`/不传为按主备顺序 |
| `topK` | int | 否 | 检索条数，1–20；不传用意图默认值 |
| `folder` | string | 否 | 单目录检索范围 |
| `folders` | string[] | 否 | 多目录检索范围，最多 20 个 |
| `responseFormat` | string | 否 | `text`（默认）或 `json` 结构化输出 |

请求示例：

```bash
curl -N http://127.0.0.1:8010/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"空压机报错 E-05 应该怎么处理？","mode":"kb"}'
```

### 4.1 事件流

事件顺序：`trace*` → `context?` → `sources?` → `token*` → `done`；异常以 `error` 收尾。

| event | data | 说明 |
| --- | --- | --- |
| `trace` | `{stage, ...}` | 执行链节点：`classify` / `retrieve` / `decide` / `attachments` / `vision_fallback` / `citation_retry` / `boundary` / `safety` |
| `context` | `{budget, used, units, citationMap, warnings, template}` | 上下文组装统计（`contextMode=shadow` 时仅观测） |
| `sources` | `{"items":[{index,docId,docName,section,page,locator,folder,score,snippet,kind?,attachmentId?}]}` | 引用来源；会话附件项带 `kind="attachment"` 与 `attachmentId` |
| `token` | `{"delta":"..."}` | 增量文本 |
| `done` | 见下 | 完成信号 |
| `error` | `{"code":42901,"message":"..."}` | 可重试错误；不会再有 `done` |

`done` 主要字段：

```json
{
  "confidence": "mid",
  "confidence_reason": "检索最高相似度 0.71；引用覆盖 67%",
  "sessionId": "4c0ed13a69ab4222917252301eee4c25",
  "messageId": 137,
  "boundary": null,
  "model": "qwen3.8-27b",
  "request_id": "2f8c1d9a7b3e",
  "plainText": "纯文本回答（用于复制 TXT）",
  "response_format": "text",
  "json_valid": null,
  "generation_params": { "temperature": 0.0, "maxTokens": 1024, "policy": "fixed" },
  "safety": { "blocked": false, "redacted": [] }
}
```

`boundary` 取值：`null`（库内回答）、`general`（非知识库内容）、`refused`（拒答）。

### 4.2 结构化输出

`responseFormat=json` 时，`token` 事件拼接后的内容是合法 JSON（schemaVersion 1.1）：

```json
{
  "schemaVersion": "1.1",
  "answer": "Markdown 回答",
  "answerPlain": "纯文本回答",
  "citations": [{ "index": 1, "docName": "...", "section": "...", "page": 30 }],
  "citationAudit": { "totalSentences": 8, "citedSentences": 8, "removedSentences": 0 },
  "confidence": "mid",
  "confidenceReason": "...",
  "boundary": null,
  "model": "qwen3.8-27b",
  "safety": { "blocked": false },
  "generationParams": { "temperature": 0.0, "maxTokens": 1536 }
}
```

### 4.3 超时与错误

- 模型流式空闲超过 `INDOCQA_LLM_STREAM_IDLE_TIMEOUT`（默认 30s）会中止并尝试备用模型；已有输出后则返回 `error 42901`。
- 检索整体超过 `INDOCQA_RETRIEVAL_TIMEOUT`（默认 45s）返回 `error 42901`。
- 前端另有 45s 无数据看门狗，会中断连接并提示重试。

---

## 5. 会话管理

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/sessions` | 新建会话 |
| GET | `/api/sessions` | 最近 50 个会话 |
| GET | `/api/sessions/{id}` | 会话详情（含消息） |
| PATCH | `/api/sessions/{id}` | 重命名 |
| DELETE | `/api/sessions/{id}` | 删除会话及其消息 |

### 5.1 新建

请求：

```json
{ "title": "空压机故障排查", "sessionId": "可选，32 位十六进制" }
```

响应：

```json
{ "data": { "id": "9ed560d4...", "title": "空压机故障排查", "taskState": null, "createdAt": "2026-11-11T11:35:16", "updatedAt": "2026-11-11T11:35:16" } }
```

### 5.2 列表

```json
{
  "data": [
    {
      "id": "9ed560d4...",
      "title": "空压机故障排查",
      "taskState": { "status": "answering", "turn": 1 },
      "createdAt": "2026-11-11T11:35:16",
      "updatedAt": "2026-11-11T11:35:18",
      "messageCount": 2,
      "lastMessage": "空压机报错 E-05 应该怎么处理？"
    }
  ]
}
```

### 5.3 详情

`GET /api/sessions/{id}` 在会话字段之外增加 `messages` 数组；单条消息字段：

`id`、`role`、`content`、`sources`、`trace`、`confidence`、`confidenceReason`、`boundary`、`model`、`contextManifest`、`attachments`、`createdAt`。

`attachments` 为用户消息的附件快照，字段包括 `id`、`sessionId`、`filename`、`mime`、`kind`、`size`、`url`、`downloadUrl`、`parseStatus`、`meta`、`textChars`。

### 5.4 重命名 / 删除

```http
PATCH /api/sessions/{id}
{ "title": "新标题" }
```

```http
DELETE /api/sessions/{id}
→ { "data": true }
```

### 5.5 会话附件上传

附件是**会话级上下文**，不写入知识库。支持图片、PDF/DOCX/MD/TXT/CSV/XLSX/PPTX、音频与视频；单文件 ≤ 100 MB，单条消息 ≤ 10 个，默认 4 MB 分片。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/uploads/init` | 初始化上传；`sessionId` 为空时自动建会话并回传 |
| PUT | `/api/uploads/{uploadId}/chunks/{index}?sessionId=...` | 上传单个分片（0-based，`application/octet-stream`，幂等可重传） |
| GET | `/api/uploads/{uploadId}?sessionId=...` | 查询已接收分片，用于断网续传 |
| POST | `/api/uploads/{uploadId}/complete` | 合并、magic 校验、解析并写入附件表 |
| DELETE | `/api/uploads/{uploadId}?sessionId=...` | 中止上传并清理 staging |
| GET | `/api/attachments/{id}/content?sessionId=...&download=0` | 读取附件内容；文档默认下载，图片/音视频/PDF 内联预览 |
| GET | `/api/sessions/{sessionId}/attachments` | 列出会话内已完成的附件 |
| DELETE | `/api/attachments/{id}?sessionId=...` | 删除附件及磁盘文件 |

初始化请求：

```json
{ "sessionId": "可选", "filename": "手册.pdf", "size": 73400320, "mime": "application/pdf" }
```

响应：

```json
{
  "data": {
    "uploadId": "9f2c...",
    "sessionId": "4c0e...",
    "chunkSize": 4194304,
    "totalChunks": 18,
    "received": [],
    "expiresAt": "2026-11-12T10:00:00"
  }
}
```

完成响应：

```json
{
  "data": {
    "id": "9f2c...",
    "sessionId": "4c0e...",
    "filename": "手册.pdf",
    "mime": "application/pdf",
    "kind": "document",
    "size": 73400320,
    "url": "/api/attachments/9f2c.../content?sessionId=4c0e...",
    "downloadUrl": "/api/attachments/9f2c.../content?sessionId=4c0e...&download=1",
    "parseStatus": "ok",
    "meta": { "pages": 42 },
    "textChars": 53210
  }
}
```

发送消息时把 `data.id` 放入 `attachmentIds`。文档文本会作为会话附件上下文参与检索与引用；图片/音视频默认提供预览与元数据，开启 `INDOCQA_ATTACHMENT_VISION` 后图片会以 `image_url` content parts 送入支持视觉的模型。

---

## 6. 知识库：入库与预检

### 6.1 同步入库

`POST /api/kb/ingest`

`multipart/form-data`：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file` | file | 是 | PDF/MD/TXT，≤20MB |
| `folder` | string | 否 | 目标目录，默认 `未分类`；目录不存在会自动创建祖先 |

响应：

```json
{
  "data": {
    "docId": "683220dead04046d",
    "filename": "螺杆式空压机操作手册.pdf",
    "pages": 48,
    "chunks": 106,
    "folder": "设备手册/螺杆空压机",
    "durationMs": 8200,
    "duplicated": false
  }
}
```

内容哈希重复时 `duplicated=true`，不会重复写入。

### 6.2 异步入库任务

`POST /api/kb/ingest/jobs`（参数同上）

```json
{ "data": { "jobId": "a1b2c3...", "totalDocs": 1, "folder": "设备手册/螺杆空压机" } }
```

`GET /api/kb/ingest/jobs/{jobId}`：

```json
{
  "data": {
    "jobId": "a1b2c3...",
    "status": "running",
    "percent": 62,
    "currentDoc": 1,
    "totalDocs": 1,
    "message": "向量化 320/516 块",
    "result": null,
    "error": null
  }
}
```

`status`：`queued` / `running` / `done` / `error`。`done` 时 `result` 与同步入库响应一致。

### 6.3 入库预检

`POST /api/kb/preview`

`multipart/form-data`：

| 字段 | 说明 |
| --- | --- |
| `file` | 待预检文件 |
| `classify` | 查询参数，`true/1` 时附带 AI 分类建议 |

响应：

```json
{
  "data": {
    "filename": "手册.pdf",
    "status": "ok",
    "pages": 48,
    "paragraphs": 420,
    "tables": 43,
    "estimatedChunks": 516,
    "totalChars": 128000,
    "charsPerPage": 2667,
    "language": "zh",
    "tocRatio": 0.12,
    "duplicated": false,
    "issues": [{ "level": "warning", "message": "疑似扫描件", "hint": "OCR 后再入库" }],
    "classification": { "available": true, "candidates": [], "suggested_new_folder": null }
  }
}
```

`status`：`ok` / `warning` / `rejected`。

---

## 7. 知识库：目录树

### 7.1 目录列表

`GET /api/kb/folders`

```json
{
  "data": [
    { "path": "设备手册", "name": "设备手册", "parent": "", "documentCount": 0, "chunkCount": 0 },
    { "path": "设备手册/螺杆空压机", "name": "螺杆空压机", "parent": "设备手册", "documentCount": 1, "chunkCount": 106 }
  ]
}
```

### 7.2 新建目录

`POST /api/kb/folders`

```json
{ "path": "设备手册/新机组" }
```

父级目录会自动创建。已有文档的目录下不能再建子目录。

### 7.3 重命名 / 移动目录

`PATCH /api/kb/folders`

```json
{ "from": "设备手册/旧名称", "to": "设备手册/新名称" }
```

不能移动到自身子目录下。

### 7.4 删除目录

`DELETE /api/kb/folders?path=设备手册/旧名称&mode=move`

| mode | 行为 |
| --- | --- |
| `move` | 结构上移：子目录与文档移动到上级目录 |
| `delete` | 级联删除：目录、子目录、文档与索引全部删除 |

默认目录 `未分类` 不允许删除。

---

## 8. 知识库：文档与移动

### 8.1 文档列表

`GET /api/kb/documents?folder=设备手册/螺杆空压机`

`folder` 可选；不传返回全部。

```json
{
  "data": [
    {
      "docId": "7b079e032f68bcb3",
      "filename": "螺杆式空压机操作手册_厂商公开.pdf",
      "folder": "设备手册/螺杆空压机",
      "chunks": 106,
      "pages": 48,
      "tables": 43,
      "ingestedAt": "2026-11-11T00:41:32"
    }
  ]
}
```

### 8.2 移动单篇文档

`POST /api/kb/documents/{docId}/move`

```json
{ "folder": "设备手册/无油空压机" }
```

```json
{ "data": { "docId": "7b079...", "folder": "设备手册/无油空压机", "operationId": "mv_...", "skipped": false } }
```

目标目录必须是末级目录；文档已在目标目录时 `skipped=true`。

### 8.3 批量移动

`POST /api/kb/documents/batch-move`

```json
{ "docIds": ["id1", "id2"], "folder": "设备手册/螺杆空压机" }
```

```json
{
  "data": {
    "operationId": "mv_1762...",
    "target": "设备手册/螺杆空压机",
    "moved": ["id1"],
    "skipped": ["id2"],
    "skippedDetails": [{ "docId": "id2", "reason": "已在目标目录" }],
    "failed": []
  }
}
```

### 8.4 撤销移动

`POST /api/kb/moves/{operationId}/undo`

撤销窗口为 10 分钟；取消或超时返回 40001。

```json
{ "data": { "operationId": "mv_...", "restored": ["id1"], "failed": [] } }
```

### 8.5 删除文档

`DELETE /api/kb/documents/{docId}`

删除文档、向量块、父块与 BM25 记录。

```json
{ "data": true }
```

---

## 9. AI 分类

需要配置输出层模型，否则返回 40904。

### 9.1 获取分类建议

`POST /api/kb/classify`

两种入参二选一：

```json
{ "docId": "683220dead04046d", "profile": "primary" }
```

```json
{ "filename": "新手册.md", "text": "文档摘要或前 4000 字", "profile": null }
```

响应：

```json
{
  "data": {
    "available": true,
    "candidates": [
      { "path": "设备手册/螺杆空压机", "confidence": 0.86, "level": "high", "reason": "标题与目录语义匹配", "evidence": ["1. 可靠性指标"] }
    ],
    "suggested_new_folder": null,
    "fallback": false,
    "reason": "",
    "latencyMs": 820,
    "model": "qwen3.8-27b"
  }
}
```

`fallback=true` 表示模型复核失败，已降级为规则/语义推荐。

### 9.2 分类反馈

`POST /api/kb/classify/feedback`

```json
{
  "docId": "683220dead04046d",
  "suggested": "设备手册/螺杆空压机",
  "chosen": "设备手册/无油空压机",
  "action": "adjust",
  "source": "doc"
}
```

`action`：`accept` / `adjust` / `reject`。反馈写入 `classification_feedback.jsonl`，用于后续规则调优。

---

## 10. 调用示例

### 10.1 完整体验：上传 → 检索 → 引用

```bash
BASE=http://127.0.0.1:8010

# 1) 上传
curl -X POST "$BASE/api/kb/ingest" \
  -F "file=@./data/corpus/fixture_术语混写_模拟维保手册.md" \
  -F "folder=评测夹具"

# 2) 提问（流式）
curl -N -X POST "$BASE/api/chat" \
  -H "Content-Type: application/json" \
  -d '{"message":"MTBF 和平均无故障时间有什么区别？","mode":"kb","topK":6}'

# 3) 查看会话
curl "$BASE/api/sessions"
```

### 10.2 Python SSE 客户端

```python
import httpx

with httpx.stream(
    "POST",
    "http://127.0.0.1:8010/api/chat",
    json={"message": "空压机报错 E-05 应该怎么处理？", "mode": "kb"},
    timeout=120,
) as response:
    event = None
    for line in response.iter_lines():
        if not line:
            event = None
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            print(event, line[5:].strip()[:120])
```

### 10.3 结构化输出

```bash
curl -N -X POST http://127.0.0.1:8010/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"储气罐的作用是什么？","mode":"kb","responseFormat":"json"}'
```

拼接所有 `token.delta` 后 `json.loads` 即可得到完整结构化答案。
