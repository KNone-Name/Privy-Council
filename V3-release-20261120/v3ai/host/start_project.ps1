# One-click launcher for the Victoria 3 AI project.
# Starts the host web in a separate window, waits for it, starts the bridge service,
# and opens http://127.0.0.1:8765.
#
# Run by double-clicking 启动项目.bat, or:
#   powershell -ExecutionPolicy Bypass -File .\v3ai\host\start_project.ps1

$ErrorActionPreference = 'Stop'
$Port = 8765
$Base = "http://127.0.0.1:$Port"
$GameAppId = '529340'   # Victoria 3

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    Write-Host '需要管理员权限（DLL 注入需要）。正在请求 UAC ...' -ForegroundColor Yellow
    $quoted = '"' + $PSCommandPath + '"'
    Start-Process powershell.exe -Verb RunAs -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $quoted
    )
    exit
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $Root
$RunLive = Join-Path $Root 'v3ai\host\run_live.ps1'

function Get-Health {
    try {
        return Invoke-RestMethod -Uri "$Base/api/service" -TimeoutSec 3 -ErrorAction Stop
    } catch {
        return $null
    }
}

Write-Host ''
Write-Host '=== Victoria 3 AI project launcher ===' -ForegroundColor Cyan
Write-Host "Root: $Root"
Write-Host "Web : $Base"
Write-Host ''

# 1. Data side check (no process; just a sanity check)
$dataSide = Join-Path (Split-Path $Root -Parent) 'Victoria3-RE'
if (-not (Test-Path $dataSide)) {
    Write-Host "[WARN] data side not found: $dataSide" -ForegroundColor Yellow
}

# 2. Game check
$launchedGame = $false
$game = Get-Process victoria3 -ErrorAction SilentlyContinue
if (-not $game) {
    $launchedGame = $true
    Write-Host 'Victoria 3 is not running. Launching through Steam ...' -ForegroundColor Yellow
    Start-Process "steam://rungameid/$GameAppId"
    for ($i = 0; $i -lt 90; $i++) {
        Start-Sleep -Seconds 2
        $game = Get-Process victoria3 -ErrorAction SilentlyContinue
        if ($game) { break }
    }
    if (-not $game) {
        Write-Host 'Could not see victoria3.exe. Start the game manually, load a save, then rerun.' -ForegroundColor Red
        Read-Host 'Press Enter to close'
        exit 1
    }
}
Write-Host "Game pid: $($game.Id)"
if (-not $game.MainWindowTitle) {
    Write-Host '[WARN] Game window title is empty; make sure the save is fully loaded.' -ForegroundColor Yellow
}
Write-Host 'Make sure Victoria 3 is in a loaded save, not the main menu.' -ForegroundColor Yellow
Read-Host 'Press Enter to start the web / bridge' | Out-Null

# 3. Reuse an existing web, otherwise start a new one in its own window.
$health = Get-Health
if ($health) {
    Write-Host 'Existing host web detected; reusing it.' -ForegroundColor Green
} else {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        Write-Host "Port $Port is already in use, but it is not the V3 web." -ForegroundColor Red
        Write-Host 'Close that process, or run with another port manually.'
        Read-Host 'Press Enter to close'
        exit 1
    }
    if (-not (Test-Path $RunLive)) {
        Write-Host "Missing $RunLive" -ForegroundColor Red
        Read-Host 'Press Enter to close'
        exit 1
    }

    Write-Host 'Starting host web in a new window ...' -ForegroundColor Cyan
    Start-Process powershell.exe -WorkingDirectory $Root -ArgumentList @(
        '-NoExit', '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', ('"' + $RunLive + '"'), '-Web', '-Port', "$Port"
    )

    Write-Host 'Waiting for prewarm / snapshot / web (up to 240s) ...'
    $health = $null
    for ($i = 0; $i -lt 120; $i++) {
        Start-Sleep -Seconds 2
        $health = Get-Health
        if ($health) { break }
    }
    if (-not $health) {
        Write-Host 'Host web did not become ready.' -ForegroundColor Red
        Write-Host 'Check the new PowerShell window for the prewarm/read error.' -ForegroundColor Yellow
        Read-Host 'Press Enter to close'
        exit 1
    }
}

# 4. Start bridge service (inject DLL + watch). It is idempotent.
Write-Host 'Starting bridge service (inject v3bridge 0.3 + watch) ...' -ForegroundColor Cyan
try {
    $start = Invoke-RestMethod -Method Post -Uri "$Base/api/service/start" `
        -ContentType 'application/json' -Body '{}' -TimeoutSec 30
    if ($start.ok -eq $false) {
        Write-Host ("Service start warning: " + $start.error) -ForegroundColor Yellow
    } else {
        Write-Host 'Service start requested.' -ForegroundColor Green
    }
} catch {
    Write-Host ("Service start failed: " + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'The web is up; open it and click 启动服务 manually.' -ForegroundColor Yellow
}

# 5. Open the dashboard.
Start-Process $Base
Write-Host ''
Write-Host 'Done. Browser opened.' -ForegroundColor Green
Write-Host 'First time: wait for scan 100% / types ready, then click 连续：开 if you want auto cycles.'
Write-Host ''
Write-Host 'Important: if the game still has the old v3inject_v109.dll loaded,'
Write-Host 'fully restart the game before starting the service, otherwise two DLLs may hook.'
Write-Host ''
Read-Host 'Press Enter to close this launcher (the web keeps running)'
