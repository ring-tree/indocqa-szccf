#!/usr/bin/env bash
# 工阅 IndocQA 一键启动（macOS / Linux）
# 用法：
#   bash scripts/start.sh
#   bash scripts/start.sh --install-only

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

for cmd in uv node npm; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "缺少命令：$cmd。请先安装并加入 PATH（参见 docs/DEPLOYMENT.md）。" >&2
    exit 1
  fi
done

echo "==> 检查配置"
if [ ! -f "$ROOT/server/.env" ]; then
  cp "$ROOT/server/.env.example" "$ROOT/server/.env"
  echo "已从 .env.example 创建 server/.env；如需问答生成，请填入真实模型 Key。" >&2
fi

echo "==> 安装后端依赖（uv sync）"
cd "$ROOT/server"
uv sync --extra dev

if [ ! -f "$ROOT/server/data/chroma/manifest.json" ]; then
  echo "==> 首次建库：写入内置语料与术语簇"
  uv run python -m app.indexing.ingest ../data/corpus
  uv run python -m app.retrieval.terms
fi

if [ "${1:-}" = "--install-only" ]; then
  echo "依赖与索引已就绪（--install-only）。"
  exit 0
fi

echo "==> 启动后端 http://127.0.0.1:8010"
uv run uvicorn app.main:app --host 127.0.0.1 --port 8010 \
  >"$LOG_DIR/backend.log" 2>"$LOG_DIR/backend.err.log" &
BACKEND_PID=$!

cleanup() {
  kill "$BACKEND_PID" "${FRONTEND_PID:-}" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8010/api/health >/dev/null 2>&1; then
    echo "后端就绪"
    break
  fi
  sleep 1
done

echo "==> 启动前端 http://127.0.0.1:5174"
cd "$ROOT/web"
if [ ! -d node_modules ]; then
  if [ -f package-lock.json ]; then npm ci; else npm install; fi
fi
VITE_API_TARGET=http://127.0.0.1:8010 VITE_PORT=5174 npm run dev \
  >"$LOG_DIR/frontend.log" 2>"$LOG_DIR/frontend.err.log" &
FRONTEND_PID=$!

echo ""
echo "工阅 IndocQA 已启动"
echo "  前端：http://127.0.0.1:5174"
echo "  接口：http://127.0.0.1:8010/docs"
echo "  日志：logs/backend.log、logs/frontend.log"
echo "  按 Ctrl+C 停止服务"

wait
