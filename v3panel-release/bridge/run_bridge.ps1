$ErrorActionPreference = "Stop"
$proc = Get-Process victoria3 -ErrorAction SilentlyContinue
if (-not $proc) { Write-Host "Victoria 3 is not running."; exit 1 }

$listening = Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue
if (-not $listening) {
    Write-Host "8766 is not listening. Start the real server first:"
    Write-Host "  python ..\server\panel_server.py"
    exit 1
}

$log = Join-Path $PSScriptRoot "v3panel_bridge.log"
$before = 0
if (Test-Path $log) { $before = [IO.File]::ReadAllText($log).Length }

$inj = Join-Path $PSScriptRoot "injector.exe"
& $inj victoria3 (Join-Path $PSScriptRoot "v3panel_bridge37.dll")

Write-Host "waiting for bridge scan (up to 5 min)..."
$deadline = (Get-Date).AddMinutes(5)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    if (-not (Test-Path $log)) { continue }
    $text = [IO.File]::ReadAllText($log)
    if ($text.Length -le $before) { continue }
    $new = $text.Substring($before)
    if ($new -match "v3ai_input = [0-9a-fA-F]+" -and $new -match "v3ai_reply = [0-9a-fA-F]+") { break }
}

$text = [IO.File]::ReadAllText($log)
if ($text.Length -gt $before) {
    Write-Host "--- new log ---"
    Write-Host $text.Substring($before)
} else {
    Write-Host "no new log output"
}