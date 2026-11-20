$flag = Join-Path $PSScriptRoot "unload_v3panel_bridge37.flag"
Set-Content -Path $flag -Value "1" -Encoding ascii
Set-Content -Path (Join-Path $PSScriptRoot "unload.flag") -Value "1" -Encoding ascii -ErrorAction SilentlyContinue
Write-Host "unload requested -> $flag"
