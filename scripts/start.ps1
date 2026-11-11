# 工阅 IndocQA 一键启动（Windows PowerShell 5+）
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts/start.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/start.ps1 -InstallOnly

param([switch]$InstallOnly)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Require-Command([string]$Name) {
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    throw "缺少命令：$Name。请先安装并加入 PATH（参见 docs/DEPLOYMENT.md）。"
  }
}

Require-Command "uv"
Require-Command "node"
Require-Command "npm"

Write-Host "==> 检查配置"
$EnvFile = Join-Path $Root "server\.env"
if (-not (Test-Path $EnvFile)) {
  Copy-Item (Join-Path $Root "server\.env.example") $EnvFile
  Write-Host "已从 .env.example 创建 server/.env；如需问答生成，请填入真实模型 Key。" -ForegroundColor Yellow
}

Write-Host "==> 安装后端依赖（uv sync）"
Push-Location (Join-Path $Root "server")
try {
  uv sync --extra dev
  if ($LASTEXITCODE -ne 0) { throw "uv sync 失败" }

  $Manifest = Join-Path $Root "server\data\chroma\manifest.json"
  if (-not (Test-Path $Manifest)) {
    Write-Host "==> 首次建库：写入内置语料与术语簇"
    uv run python -m app.indexing.ingest ..\data\corpus
    if ($LASTEXITCODE -ne 0) { throw "建库失败" }
    uv run python -m app.retrieval.terms
    if ($LASTEXITCODE -ne 0) { throw "术语簇构建失败" }
  }
} finally {
  Pop-Location
}

if ($InstallOnly) {
  Write-Host "依赖与索引已就绪（-InstallOnly）。"
  exit 0
}

Write-Host "==> 启动后端 http://127.0.0.1:8010"
$Backend = Start-Process -FilePath (Get-Command "uv").Source `
  -ArgumentList @("run", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8010") `
  -WorkingDirectory (Join-Path $Root "server") -PassThru `
  -RedirectStandardOutput (Join-Path $LogDir "backend.log") `
  -RedirectStandardError (Join-Path $LogDir "backend.err.log")

$Healthy = $false
for ($i = 0; $i -lt 60; $i++) {
  Start-Sleep -Seconds 1
  try {
    $Health = Invoke-RestMethod -Uri "http://127.0.0.1:8010/api/health" -TimeoutSec 3
    $Healthy = $true
    Write-Host ("后端就绪：{0} 份文档 / {1} 块，嵌入 {2}" -f $Health.kb.documents, $Health.kb.chunks, $Health.embedding.backend)
    break
  } catch {
    if ($Backend.HasExited) { throw "后端启动失败，请查看 logs/backend.err.log" }
  }
}
if (-not $Healthy) { throw "后端 60 秒内未就绪，请查看 logs/backend.err.log" }

Write-Host "==> 启动前端 http://127.0.0.1:5174"
Push-Location (Join-Path $Root "web")
try {
  if (-not (Test-Path "node_modules")) {
    if (Test-Path "package-lock.json") { npm ci } else { npm install }
    if ($LASTEXITCODE -ne 0) { throw "前端依赖安装失败" }
  }
} finally {
  Pop-Location
}

$env:VITE_API_TARGET = "http://127.0.0.1:8010"
$env:VITE_PORT = "5174"
$Frontend = Start-Process -FilePath "npm.cmd" -ArgumentList @("run", "dev") `
  -WorkingDirectory (Join-Path $Root "web") -PassThru `
  -RedirectStandardOutput (Join-Path $LogDir "frontend.log") `
  -RedirectStandardError (Join-Path $LogDir "frontend.err.log")

Write-Host ""
Write-Host "工阅 IndocQA 已启动" -ForegroundColor Green
Write-Host "  前端：http://127.0.0.1:5174"
Write-Host "  接口：http://127.0.0.1:8010/docs"
Write-Host "  日志：logs/backend.log、logs/frontend.log"
Write-Host ("  PID：backend={0} frontend={1}（关闭窗口不会自动结束进程）" -f $Backend.Id, $Frontend.Id)
