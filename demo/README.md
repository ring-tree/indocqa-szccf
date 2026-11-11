# IndocQA 三场景现场演示物料包

本目录把赛题中的三个示例场景整理为**可直接运行**的演示物料：问题、语料、生成的长文档/图片/表格、运行脚本与验收标准。

## 1. 目录结构

```text
demo/
├── README.md                         # 本说明
├── run_demo.py                       # 三场景自动运行器（调用本机 API）
├── smoke_check.py                    # 核心接口 smoke check
├── scenario1/
│   └── questions.json                # 场景1：术语解释
├── scenario2/
│   └── questions.json                # 场景2：故障排查
├── scenario3/
│   ├── questions.json                # 场景3：长文档与图表
│   ├── 产线设备维护手册_演示.pdf        # 120页，含图5-3、表6-1
│   ├── 图5-3_3号产线润滑点位图.png     # 12个润滑点位示意图
│   └── 表6-1_保养周期表.csv            # 3号产线：500小时/12处
└── tools/
    └── build_scenario3_assets.py      # 重新生成场景3物料
```

## 2. 三个场景

| 场景 | 问题 | 物料 | 预期结果 |
|---|---|---|---|
| 1 术语解释 | 设备手册里的 MTBF 和维修规程里的平均无故障时间是一回事吗？额定值是多少？ | `data/corpus/fixture_术语混写_模拟维保手册.md` | MTBF 即平均无故障时间，二者同一指标；ZX-50 额定值不低于 8000 小时 |
| 2 故障排查 | 空压机报错代码 E-05 应该怎么处理？请给出 S-12 的具体操作步骤。 | 同上 | E-05 = 排气温度过高；处置步骤编号 S-12；含停机、检查油位、清理油冷器、检查温控阀、复位重启 |
| 3 长文档与图表 | 附件中的3号产线润滑点位图和保养周期分别是多少？ | 120页演示PDF + 润滑点位图PNG + 保养周期CSV | 3号产线共12处润滑点位（图5-3）；保养周期每500运行小时一次（表6-1） |

场景 1/2 使用知识库中的评测夹具；场景 3 通过**会话附件**上传，不写入全局知识库。

## 3. 环境与依赖

- Python ≥ 3.11、`uv`
- Node.js 20+、npm（仅前端演示需要）
- 后端依赖：`cd server && uv sync --extra dev`
- 前端依赖：`cd web && npm ci`
- 生成场景 3 物料需要 `pymupdf`、`Pillow`（已在后端依赖中）

`.env` 使用项目标准配置即可（模型网关、嵌入后端、附件目录等）。`.env.example` 是模板；当前 `.env` 已按模板对齐，包含 `INDOCQA_ATTACHMENT_*` 等配置。

## 4. 启动

```powershell
# 方式 A：一键启动前后端
powershell -ExecutionPolicy Bypass -File scripts/start.ps1

# 方式 B：手动启动
cd server
uv run uvicorn app.main:app --host 127.0.0.1 --port 8010

cd web
npm run dev
```

默认地址：

- 后端：<http://127.0.0.1:8010>
- 前端：<http://127.0.0.1:5174>
- 接口文档：<http://127.0.0.1:8010/docs>

> 修改代码或 `.env` 后必须重启后端，否则仍运行旧进程。

## 5. 运行演示

```powershell
# 三个场景全部运行（缺少评测夹具时自动入库）
uv run --project server python demo/run_demo.py --base-url http://127.0.0.1:8010

# 单独运行某个场景
uv run --project server python demo/run_demo.py --scenario 3
```

脚本输出每个场景的问题、回答、来源、置信度和自动检查结果；三个场景会分别创建会话并保留在侧栏，便于现场点击查看。

### 核心接口 smoke check

```powershell
uv run --project server python demo/smoke_check.py --base-url http://127.0.0.1:8010
```

覆盖：

- `GET /api/health`
- `GET /api/models`
- `GET /api/kb/folders`、`GET /api/kb/documents`
- `POST /api/kb/preview`
- `POST /api/kb/ingest`（使用评测夹具，幂等）
- 会话 CRUD
- 附件 `init → chunk → status → complete → content → list → delete`
- 附件中止/清理、格式拒绝 `40003`
- `POST /api/chat` SSE

预检/入库/附件测试会创建临时文件与会话，结束后删除；不会删除知识库文档。

## 6. 现场 UI 演示流程

1. 打开 <http://127.0.0.1:5174>，侧栏选择“演示1-术语解释”，展示回答、来源（夹具文档）与执行链。
2. 选择“演示2-故障排查”，展示 E-05 表格行与 S-12 步骤。
3. 选择“演示3-过百页长文档与图表”：
   - 用户消息中可见 120 页 PDF 文档卡片、润滑点位图缩略图、保养周期 CSV 卡片；
   - 点击图片可预览 12 个润滑点位；
   - 点击 PDF 可打开 120 页手册；
   - 来源面板显示“会话附件”，包含 PDF/PNG/CSV；
   - 回答给出 12 处点位与 500 小时周期。
4. 可在同一会话继续追问，验证会话附件上下文复用。

## 7. 场景 3 物料说明与边界

- `产线设备维护手册_演示.pdf` 为**团队自造的 120 页模拟文档**，不是真实厂商资料；封面已标注“虚构文档，仅用于演示”。
- 第 2 页为“第5章 润滑系统与点位布置”，含 `图5-3 3号产线润滑点位图（共12处润滑点位）` 与文字说明；第 3 页为“第6章 保养周期与耗材”，含 `表6-1`（3号产线 500 小时 / 12 处）。
- 当前 PDF 内嵌图片默认**不做 OCR**；回答依据图注文字与表 6-1/CSV 文本，图片本身用于 PDF 内预览和独立 PNG 附件预览。若现场启用了支持视觉的模型并设置 `INDOCQA_ATTACHMENT_VISION=true`，图片会以 `image_url` 送入模型；未启用时模型只能看到图片元数据，这是预期行为。
- 独立 PNG 与 CSV 用于让 UI 直观看到图片/表格附件；CSV 同时保证“表 6-1”内容在短文本附件中可稳定检索。

重新生成场景 3 物料：

```powershell
uv run --project server python demo/tools/build_scenario3_assets.py
```

## 8. 常见问题

| 现象 | 处理 |
|---|---|
| 场景 1/2 报 `40401 知识库为空` | 运行器会自动入库夹具；也可手动 `cd server && uv run python -m app.indexing.ingest ../data/corpus` |
| `42901` 模型不可用 | 检查 `.env` 的 `INDOCQA_LLM_BASE_URL / API_KEY / MODEL`，重启后端；未配置模型时知识库问答会降级为原文摘录/拒答 |
| 场景 3 附件上传失败 | 检查 `server/data/attachments` 写权限；单文件 ≤100MB；PDF 21MB 会分 6 片上传 |
| 端口冲突 | 后端 8010、前端 5174；确认旧进程已退出 |
| 场景 3 回答没有“12/500” | 先运行 `smoke_check.py` 确认附件上传；再检查 PDF 是否被替换为旧文件；可重新生成物料 |
| 回答内容与示例不完全一致 | 模型生成有随机性；以“是否包含 8000 / 排气温度过高 / S-12 / 12 / 500”与来源命中为验收标准 |

## 9. 最小验收命令

```powershell
# 1. 后端测试
cd server
uv run pytest -q

# 2. 前端构建
cd ../web
npm run lint
npm run build

# 3. 服务健康 + 核心接口
cd ..
uv run --project server python demo/smoke_check.py

# 4. 三场景
uv run --project server python demo/run_demo.py
```
