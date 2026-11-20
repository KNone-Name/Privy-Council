param(
    [string]$Old = "v3panel_bridge37.dll",
    [string]$New = "v3panel_bridge37.dll"
)
$ErrorActionPreference = "Stop"
$dir = $PSScriptRoot
$proc = Get-Process victoria3 -ErrorAction SilentlyContinue
if (-not $proc) { Write-Host "Victoria 3 is not running."; exit 1 }
$inj = Join-Path $PSScriptRoot "injector.exe"

$oldBase = [IO.Path]::GetFileNameWithoutExtension($Old)
foreach ($name in @("unload.flag", "unload_$oldBase.flag")) {
    Set-Content -Path (Join-Path $dir $name) -Value "1" -Encoding ascii
}
Write-Host "unload requested for $Old"

$oldPath = Join-Path $dir $Old
$unlocked = $false
for ($i = 0; $i -lt 400; $i++) {
    Start-Sleep -Milliseconds 500
    if (-not (Test-Path $oldPath)) { $unlocked = $true; break }
    try {
        $fs = [IO.File]::Open($oldPath, 'Open', 'ReadWrite', 'None')
        $fs.Close()
        $unlocked = $true
        break
    } catch { }
}
if (-not $unlocked) {
    Write-Host "old DLL still locked (bridge busy?); try again"
    exit 2
}

$log = Join-Path $dir "v3panel_bridge.log"
Remove-Item (Join-Path $dir "v3panel_objects.txt") -Force -ErrorAction SilentlyContinue
$before = 0
if (Test-Path $log) { $before = [IO.File]::ReadAllText($log).Length }

Write-Host "old bridge unloaded; injecting $New"
& $inj victoria3 (Join-Path $dir $New)

Write-Host "waiting for bridge scan (up to 5 min)..."
$deadline = (Get-Date).AddMinutes(5)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 1
    if (-not (Test-Path $log)) { continue }
    $text = [IO.File]::ReadAllText($log)
    if ($text.Length -le $before) { continue }
    $new = $text.Substring($before)
    if ($new -match "v3ai_input = [0-9a-fA-F]+" -and $new -match "v3ai_reply = [0-9a-fA-F]+") {
        break
    }
}
$text = [IO.File]::ReadAllText($log)
if ($text.Length -gt $before) {
    Write-Host "--- new log ---"
    Write-Host $text.Substring($before)
} else {
    Write-Host "no new log output"
}