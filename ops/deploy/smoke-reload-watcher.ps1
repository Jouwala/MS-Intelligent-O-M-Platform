<#
  smoke-reload-watcher.ps1 — 改完 smoke-watcher 后一条命令重载常驻进程
  用法：powershell -NoProfile -File ops\deploy\smoke-reload-watcher.ps1
#>
$ErrorActionPreference = 'Stop'
$install = Join-Path $PSScriptRoot 'install-smoke-watcher.ps1'
if (-not (Test-Path -LiteralPath $install)) { throw "找不到 $install" }
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $install -Start
Write-Host ''
Write-Host '建议立刻看队列：'
Write-Host '  powershell -NoProfile -File ops\deploy\smoke-queue-status.ps1'