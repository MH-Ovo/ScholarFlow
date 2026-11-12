# 确保知网 MCP 服务（cnki-mcp serve）处于可用状态：未运行时拉起并等待端口就绪。
#
# 背景：知网检索走 MCP 常驻服务（默认 127.0.0.1:7788/mcp），
# 该进程会随终端关闭或异常退出而消失。不可用时 kb 之外的知识源会静默降级为
# site:cnki.net 搜索，结果质量明显下降（标题被截断、甚至串到无关页面），
# 且从外部看只是"知网没搜到内容"，排查成本很高。
#
# 前置条件：本机 .venv 内已安装 cnki-mcp，且已完成过一次机构认证
#   .venv\Scripts\cnki-mcp.exe login --profile school
# （校园网 IP 已认证时会直接返回 already logged in，无需人工操作）
#
# 用法：powershell -ExecutionPolicy Bypass -File infra\ensure-cnki.ps1

param(
    [int]$TimeoutSec = 90
)

$ErrorActionPreference = 'Stop'

$root = Split-Path $PSScriptRoot -Parent
$exe = Join-Path $root '.venv\Scripts\cnki-mcp.exe'
$port = 7788
$logDir = Join-Path $env:TEMP 'sf_cnki_serve.log'

function Test-Port {
    param([int]$Port)
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(800)
        if ($ok) { $client.EndConnect($iar) }
        $client.Close()
        return $ok
    } catch {
        return $false
    }
}

if (Test-Port $port) {
    Write-Host "[cnki] already running on $port"
    exit 0
}

if (-not (Test-Path $exe)) {
    Write-Error "[cnki] cnki-mcp.exe not found at $exe"
    exit 1
}

Start-Process -FilePath $exe `
    -ArgumentList 'serve', '--transport', 'http', '--host', '127.0.0.1', '--port', $port `
    -WorkingDirectory $root `
    -RedirectStandardOutput $logDir `
    -RedirectStandardError (Join-Path $env:TEMP 'sf_cnki_serve.err') `
    -WindowStyle Hidden | Out-Null

Write-Host "[cnki] launching mcp serve on $port ..."

$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 800
    if (Test-Port $port) {
        Write-Host '[cnki] ready'
        exit 0
    }
}

Write-Error "[cnki] not ready within ${TimeoutSec}s (check $logDir)"
exit 1
