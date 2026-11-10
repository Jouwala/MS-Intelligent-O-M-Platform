# post_sync_fix_paths.ps1 -- re-apply copy-local path fixes after a source sync.
#
# Why: build_olbr_portable.ps1 -Stage source copies Task/my_app and the portal
# subtree verbatim from D:\01_RPA, so two scripts that were hand-fixed for the
# isolated copy get reverted to D:\01_RPA paths on every sync:
#
#   1) Task\my_app\monitor_fixes\nginx_monitor.ps1   (NginxExe/NginxDir/Port/Log/State)
#   2) <portal>\rotate_nginx_logs.ps1                ($Prefix)
#
# Reverting them is not fatal while the copy is idle, but at cutover the nginx
# watchdog would probe D:\01_RPA\nginx-1.28.0\nginx.exe (port 80) instead of the
# copy's own instance, and could spawn a second nginx that fights for the port.
#
# Idempotent: safe to run any number of times. ASCII only, CRLF.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\post_sync_fix_paths.ps1

[CmdletBinding()]
param(
    [string]$CopyRoot = ''
)

$ErrorActionPreference = 'Stop'
if (-not $CopyRoot) {
    # $PSScriptRoot can be empty during param binding in some hosts; resolve here.
    $CopyRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
    $CopyRoot = Split-Path -Parent $CopyRoot
}
if (-not (Test-Path -LiteralPath (Join-Path $CopyRoot 'ISOLATED_COPY.flag'))) {
    throw "Not an isolated copy root: $CopyRoot"
}
$changed = 0

function Read-EnvPort {
    param([string]$Key, [int]$Default)
    $envFile = Join-Path $CopyRoot '.env'
    if (-not (Test-Path -LiteralPath $envFile)) { return $Default }
    foreach ($line in Get-Content -LiteralPath $envFile) {
        if ($line -match ('^\s*' + [regex]::Escape($Key) + '\s*=\s*(\d+)')) { return [int]$Matches[1] }
    }
    return $Default
}

$httpPort = Read-EnvPort -Key 'NGINX_HTTP_PORT' -Default 8080
$nginxDir = Join-Path $CopyRoot 'nginx-1.28.0'
$nginxExe = Join-Path $nginxDir 'nginx.exe'
$logPath = Join-Path $CopyRoot 'Task\nginx_monitor.log'
$statePath = Join-Path $CopyRoot 'Task\nginx_monitor.state'

# ---------------------------------------------------------------- 1) nginx_monitor
$mon = Join-Path $CopyRoot 'Task\my_app\monitor_fixes\nginx_monitor.ps1'
if (Test-Path -LiteralPath $mon) {
    $text = Get-Content -LiteralPath $mon -Raw
    $orig = $text
    # Only rewrite the default values inside the param() block.
    $text = [regex]::Replace($text, '(\[string\]\$NginxExe\s*=\s*")[^"]*(")', ('${1}' + $nginxExe + '${2}'))
    $text = [regex]::Replace($text, '(\[string\]\$NginxDir\s*=\s*")[^"]*(")', ('${1}' + $nginxDir + '${2}'))
    $text = [regex]::Replace($text, '(\[int\]\$NginxPort\s*=\s*)\d+', ('${1}' + $httpPort))
    $text = [regex]::Replace($text, '(\[string\]\$LogPath\s*=\s*")[^"]*(")', ('${1}' + $logPath + '${2}'))
    $text = [regex]::Replace($text, '(\[string\]\$StatePath\s*=\s*")[^"]*(")', ('${1}' + $statePath + '${2}'))
    if ($text -ne $orig) {
        Set-Content -LiteralPath $mon -Value $text -Encoding UTF8 -NoNewline
        Write-Host "[FIX] nginx_monitor.ps1 -> copy paths, port $httpPort" -ForegroundColor Green
        $changed++
    } else {
        Write-Host "[OK ] nginx_monitor.ps1 already points to the copy" -ForegroundColor DarkGray
    }
} else {
    Write-Host "[MISS] $mon" -ForegroundColor DarkYellow
}

# ------------------------------------------------------------ 2) rotate_nginx_logs
# The portal folder name is Chinese, so locate it by probing for the script
# instead of hardcoding a literal (PS 5.1 reads .ps1 as ANSI without a BOM).
$rot = $null
foreach ($d in (Get-ChildItem -LiteralPath $CopyRoot -Directory -ErrorAction SilentlyContinue)) {
    $cand = Join-Path $d.FullName 'rotate_nginx_logs.ps1'
    if (Test-Path -LiteralPath $cand) { $rot = $cand; break }
}
if ($rot) {
    $text = Get-Content -LiteralPath $rot -Raw
    $orig = $text
    $text = [regex]::Replace($text, "(\[string\]\`$Prefix\s*=\s*')[^']*(')", ('${1}' + $nginxDir.TrimEnd('\') + '\${2}'))
    if ($text -ne $orig) {
        Set-Content -LiteralPath $rot -Value $text -Encoding UTF8 -NoNewline
        Write-Host "[FIX] rotate_nginx_logs.ps1 `$Prefix -> $nginxDir" -ForegroundColor Green
        $changed++
    } else {
        Write-Host "[OK ] rotate_nginx_logs.ps1 already points to the copy" -ForegroundColor DarkGray
    }
} else {
    Write-Host "[MISS] $rot" -ForegroundColor DarkYellow
}

Write-Host ("Done. {0} file(s) changed." -f $changed) -ForegroundColor Cyan
