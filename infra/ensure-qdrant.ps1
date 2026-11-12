# 确保本地 Qdrant 处于可用状态：未运行时静默拉起并等待就绪。
#
# 背景：本项目用 Qdrant 官方 Windows 二进制（非 Docker）运行，进程可能随终端
# 关闭或异常退出。kb_search 在 Qdrant 不可用时会静默降级为空结果，表现为
# "知识库检索不到内容"，排查成本很高。调研前先跑一次本脚本可避免该问题。
#
# 用法：powershell -ExecutionPolicy Bypass -File infra\ensure-qdrant.ps1

param(
    [int]$TimeoutSec = 30
)

$ErrorActionPreference = 'Stop'

$exe = Join-Path $env:LOCALAPPDATA 'qdrant\qdrant.exe'
$readyUrl = 'http://127.0.0.1:6333/readyz'

function Test-QdrantReady {
    try {
        $r = Invoke-WebRequest -Uri $readyUrl -TimeoutSec 3 -UseBasicParsing
        return $r.StatusCode -eq 200
    } catch {
        return $false
    }
}

if (Test-QdrantReady) {
    Write-Host '[qdrant] already running on 6333'
    exit 0
}

if (-not (Test-Path $exe)) {
    Write-Error "[qdrant] qdrant.exe not found at $exe"
    exit 1
}

$workdir = Split-Path $exe -Parent
Start-Process -FilePath $exe -WorkingDirectory $workdir -WindowStyle Hidden | Out-Null
Write-Host "[qdrant] launching from $workdir ..."

$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 800
    if (Test-QdrantReady) {
        Write-Host '[qdrant] ready'
        exit 0
    }
}

Write-Error "[qdrant] not ready within ${TimeoutSec}s"
exit 1
