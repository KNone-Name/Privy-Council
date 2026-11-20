# 在同一 Windows 会话里跑 v3ai（能看到 victoria3.exe 的完整内存）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\v3ai\host\run_live.ps1
#   powershell -ExecutionPolicy Bypass -File .\v3ai\host\run_live.ps1 -Run
#   powershell -ExecutionPolicy Bypass -File .\v3ai\host\run_live.ps1 -Run -Mode bridge
#
# 注意：必须在“游戏所在的那个 Windows 会话/权限级别”运行；
# 如果在 DSH 沙箱里跑，v3live 可能看不到 victoria3.exe（但 bridge 文件仍可用）。

param(
    [switch]$Run,
    [switch]$Web,
    [ValidateSet('dry_run', 'bridge', 'mock')]
    [string]$Mode = 'dry_run',
    [int]$Cycles = 1,
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
Set-Location $root

if ($Web) {
    Write-Host '[WEB] 先预热并生成本地快照，再启动 web（避免 --live 初始化卡住）...'
    python v3.py prewarm
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    python v3.py read --json snap_live.json
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host '[WEB] 启动 web（同一游戏会话；一键启动/停止 DLL 可用）...'
    python -m v3ai web --snapshot snap_live.json --port $Port
    exit $LASTEXITCODE
}

$proc = Get-Process victoria3 -ErrorAction SilentlyContinue
if (-not $proc) {
    Write-Host '[WARN] 当前会话看不到 victoria3.exe；live 读取会失败。' -ForegroundColor Yellow
    Write-Host '       请确认游戏已启动、已加载存档，并且本终端与游戏在同一会话/权限级别。'
} else {
    Write-Host ('[OK] victoria3.exe pid={0}' -f $proc.Id) -ForegroundColor Green
}

Write-Host '[1/3] prewarm（游戏重启后只需一次）...'
python v3.py prewarm

Write-Host '[2/3] 读取 live 快照 -> snap_live.json ...'
python v3.py read --json snap_live.json
if (Test-Path '.\snap_live.json') {
    $snap = Get-Item '.\snap_live.json'
    Write-Host ('[OK] snap_live.json {0:N0} bytes, {1}' -f $snap.Length, $snap.LastWriteTime) -ForegroundColor Green
}

Write-Host '[3/3] live 自检...'
python -m v3ai doctor --live

if ($Run) {
    Write-Host ('[RUN] 跑 {0} 轮决策，mode={1} ...' -f $Cycles, $Mode)
    python -m v3ai run --live --mode $Mode --cycles $Cycles
} else {
    Write-Host '[DONE] 快照已生成；把 snap_live.json 交给 DSH 侧，或加 -Run 在本会话直接跑决策循环。'
}
