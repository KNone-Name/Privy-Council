@echo off
setlocal
set "SCRIPT=%~dp0v3ai\host\start_project.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%"
if errorlevel 1 pause
endlocal
