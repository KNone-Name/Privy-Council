# 在“游戏同一 Windows 会话”里启动主机代理。
#
# 作用：让 DSH 沙箱里的 web 页面可以一键 inject / unload DLL、启动/停止快照 watch。
# 这个脚本本身不注入；它只负责收发工作区里的 JSON 指令。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\v3ai\host\start_host_agent.ps1
#
# 需要管理员时，请用“以管理员身份运行”的 PowerShell 再执行。

$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
Set-Location $root

$victoria = Get-Process victoria3 -ErrorAction SilentlyContinue
if (-not $victoria) {
    Write-Host '[WARN] 当前会话看不到 victoria3.exe；请确认游戏已启动、已加载存档，' -ForegroundColor Yellow
    Write-Host '       并且本终端与游戏在同一 Windows 会话/权限级别。'
} else {
    Write-Host ('[OK] victoria3.exe pid={0}' -f $victoria.Id) -ForegroundColor Green
}

Write-Host '启动 host agent（Ctrl+C 退出）...'
python -m v3ai.host_agent
