<#
  snapshot-prod-state.ps1 —— 生产切换前的「只读」状态快照
  ============================================================================
  为什么需要它：切换（D:\01_olbr_portal → D:\02_olbr_portal）会停生产进程、
  改 02 的 .env 端口、禁用 01 的守护任务。一旦要回退，必须知道「回退到什么」。
  这个脚本只读，不动任何进程/任务/文件，把当时的事实落成一份快照：

    <快照目录>\
      state.json         机器可读（回退脚本只认它）
      report.md          人可读（切前会签用）
      02-nginx.conf      02 当前 nginx 配置（切换后会被重生成，留档对照）
      01-nginx.conf      01 当前 nginx 配置（切换不应改动它，留档证明）

  记录内容：
    · 生产/隔离两套端口各由哪个进程占用（exe + 命令行）
    · 01 守护类计划任务的状态与动作（切换时会被禁用，回退时要恢复）
    · 02 .env 里与切换相关的 7 个键的旧值（端口 + 调度器开关；不含任何密钥）
    · 三张本地权威表行数（01 / 02 两侧）
    · 两侧 git HEAD

  用法（只读，随时可跑）：
    powershell -NoProfile -File ops\deploy\cutover\snapshot-prod-state.ps1
    powershell -NoProfile -File ops\deploy\cutover\snapshot-prod-state.ps1 -OutDir D:\02_olbr_portal\TMP\cutover\manual-1

  注意：快照目录默认落在 TMP\cutover\ 下（TMP 已 gitignore），**不要入库**。
#>
[CmdletBinding()]
param(
    [string]$OutDir = '',
    [int[]]$ProdPorts = @(80, 443, 7038, 7090, 9267, 5432),
    [int[]]$IsoPorts = @(8080, 8443, 7039, 7091, 9277, 55432)
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

function Write-Step { param([string]$m) Write-Host "[*] $m" }
function Write-Ok { param([string]$m) Write-Host "[OK] $m" -ForegroundColor Green }
function Write-Warn2 { param([string]$m) Write-Host "[WARN] $m" -ForegroundColor Yellow }

$Root02 = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..\..')).Path
$Root01 = 'D:\01_olbr_portal'
if ($Root02 -notlike '*02_olbr_portal') { throw "当前脚本不在 02 副本内（$Root02），拒绝执行。" }
if (-not (Test-Path -LiteralPath $Root01)) { throw "找不到生产副本 $Root01。" }

if (-not $OutDir) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $OutDir = Join-Path $Root02 ("TMP\cutover\{0}" -f $stamp)
}
if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path
Write-Step "快照目录：$OutDir"

# ---------- 端口归属 ----------
function Get-PortOwner {
    param([int]$Port)
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) { return $null }
    $proc = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $conn.OwningProcess) -ErrorAction SilentlyContinue
    return [pscustomobject]@{
        port = $Port
        pid  = [int]$conn.OwningProcess
        exe  = $(if ($proc) { $proc.ExecutablePath } else { '' })
        cmd  = $(if ($proc) { $proc.CommandLine } else { '' })
    }
}

Write-Step '采集端口归属'
$ports = @()
foreach ($p in @($ProdPorts + $IsoPorts)) {
    $o = Get-PortOwner -Port $p
    if ($o) { $ports += $o; Write-Ok ("{0} <- pid {1} {2}" -f $p, $o.pid, $o.exe) }
    else { Write-Host ("    {0} 空闲" -f $p) -ForegroundColor DarkGray }
}

# ---------- 守护任务 ----------
# 这些任务会在切换时被禁用（否则它们 5 分钟内就把 01 拉回来、和 02 抢端口），
# 回退脚本按快照里的原状态恢复。
$TaskNames = @(
    'MyApp_AutoStart',
    'MyApp_EnsureRunning',
    'MyApp_AuthHealthCheck',
    'Nginx_EnsureRunning',
    'Portal_EnsureRunning',
    'WhatsAppKB_Bot_EnsureRunning',
    'WhatsAppKB_Notify_EnsureRunning',
    'Nginx_LogRotate'
)

Write-Step '采集计划任务'
$tasks = @()
foreach ($n in $TaskNames) {
    $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
    if (-not $t) { Write-Warn2 "任务不存在：$n"; continue }
    $act = $t.Actions | ForEach-Object { ("{0} {1}" -f $_.Execute, $_.Arguments).Trim() }
    $tasks += [pscustomobject]@{
        name    = $n
        state   = [string]$t.State
        actions = @($act)
    }
    Write-Ok ("{0} = {1}" -f $n, $t.State)
}

# ---------- 02 的 .env（只取与切换相关的键，不含密钥） ----------
$EnvKeys = @(
    'NGINX_HTTP_PORT', 'NGINX_HTTPS_PORT', 'MY_APP_BE_PORT', 'MY_APP_FE_PORT',
    'PORTAL_PORT', 'PGPORT', 'MY_APP_DISABLE_SCHEDULER'
)
Write-Step '采集 02 的 .env（仅端口/开关这 7 个键）'
$envMap = [ordered]@{}
$envPath = Join-Path $Root02 '.env'
if (Test-Path -LiteralPath $envPath) {
    foreach ($line in [System.IO.File]::ReadAllLines($envPath, [System.Text.Encoding]::UTF8)) {
        if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
        $k = ($line -split '=', 2)[0].Trim()
        $v = ($line -split '=', 2)[1].Trim()
        if ($EnvKeys -contains $k) { $envMap[$k] = $v; Write-Ok ("{0}={1}" -f $k, $v) }
    }
    foreach ($k in $EnvKeys) { if (-not $envMap.Contains($k)) { Write-Warn2 "02 .env 缺键：$k" } }
} else {
    Write-Warn2 "02 没有 .env：$envPath"
}

# ---------- nginx 配置留档 ----------
Write-Step '留档两侧 nginx.conf'
$confPairs = @(
    @{ name = '02-nginx.conf'; path = (Join-Path $Root02 'nginx-1.28.0\conf\nginx.conf') },
    @{ name = '01-nginx.conf'; path = (Join-Path $Root01 'nginx-1.28.0\conf\nginx.conf') }
)
foreach ($c in $confPairs) {
    if (Test-Path -LiteralPath $c.path) {
        Copy-Item -LiteralPath $c.path -Destination (Join-Path $OutDir $c.name) -Force
        Write-Ok ("{0}（{1} 字节）" -f $c.name, (Get-Item -LiteralPath $c.path).Length)
    } else {
        Write-Warn2 "缺配置：$($c.path)"
    }
}

# ---------- 三张本地权威表 ----------
Write-Step '核对三张本地权威表（只读）'
$db01 = Join-Path $Root01 'Task\my_app\db\app.db'
$db02 = Join-Path $Root02 'apps\my_app\db\app.db'
$authRows = @()
$pyExe = Join-Path $Root02 'runtime\python312\python.exe'
$pyCode = @'
import sqlite3, json, sys
out = {}
for label, p in [("01", sys.argv[1]), ("02", sys.argv[2])]:
    d = {}
    try:
        c = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
        for t in ("credentials", "task_schedule_config", "email_recipients"):
            d[t] = c.execute("select count(*) from %s" % t).fetchone()[0]
        c.close()
    except Exception as e:
        d["error"] = str(e)
    out[label] = d
print(json.dumps(out, ensure_ascii=False))
'@
if ((Test-Path -LiteralPath $db01) -and (Test-Path -LiteralPath $db02) -and (Test-Path -LiteralPath $pyExe)) {
    $json = $pyCode | & $pyExe - $db01 $db02 2>&1
    Write-Host ("    " + ($json -join ' '))
    try { $authRows = ($json -join '') | ConvertFrom-Json } catch { $authRows = $null }
} else {
    Write-Warn2 '缺 app.db 或 runtime python，跳三表核对'
}

# ---------- git HEAD ----------
function Get-GitHead {
    param([string]$Dir)
    try { return (& git -C $Dir rev-parse --short HEAD 2>$null) } catch { return '' }
}
$git02 = Get-GitHead $Root02
$git01 = Get-GitHead $Root01

# ---------- 落盘 ----------
$state = [pscustomobject]@{
    capturedAt  = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss')
    host        = $env:COMPUTERNAME
    user        = "$env:USERDOMAIN\$env:USERNAME"
    root01      = $Root01
    root02      = $Root02
    gitHead01   = $git01
    gitHead02   = $git02
    ports       = @($ports)
    tasks       = @($tasks)
    env02       = $envMap
    authority   = $authRows
}
$statePath = Join-Path $OutDir 'state.json'
$state | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statePath -Encoding UTF8
Write-Ok "已写 $statePath"

$md = New-Object System.Collections.Generic.List[string]
$md.Add('# 生产切换前状态快照')
$md.Add('')
$md.Add(('- 采集时间：{0}（{1}\{2}）' -f $state.capturedAt, $env:COMPUTERNAME, $env:USERNAME))
$md.Add(('- 02 HEAD：`{0}`　01 HEAD：`{1}`' -f $git02, $git01))
$md.Add('')
$md.Add('## 端口归属')
$md.Add('')
$md.Add('| 端口 | pid | 可执行文件 |')
$md.Add('| --- | --- | --- |')
foreach ($p in $ports) { $md.Add(('| {0} | {1} | `{2}` |' -f $p.port, $p.pid, $p.exe)) }
$md.Add('')
$md.Add('## 计划任务（回退时按此恢复）')
$md.Add('')
$md.Add('| 任务 | 切换前状态 | 动作 |')
$md.Add('| --- | --- | --- |')
foreach ($t in $tasks) { $md.Add(('| {0} | {1} | `{2}` |' -f $t.name, $t.state, ($t.actions -join ' ; '))) }
$md.Add('')
$md.Add('## 02 .env（与切换相关的键）')
$md.Add('')
$md.Add('| 键 | 切换前值 |')
$md.Add('| --- | --- |')
foreach ($k in $envMap.Keys) { $md.Add(('| `{0}` | `{1}` |' -f $k, $envMap[$k])) }
$md.Add('')
$md.Add('## 三张本地权威表行数')
$md.Add('')
$md.Add('```json')
$md.Add(($authRows | ConvertTo-Json -Depth 5))
$md.Add('```')
$md.Add('')
$md.Add('> 本目录含运行态信息，**不入库**（TMP 已 gitignore）。回退脚本：`ops\deploy\cutover\rollback-to-01.ps1`。')
$reportPath = Join-Path $OutDir 'report.md'
$md | Set-Content -LiteralPath $reportPath -Encoding UTF8
Write-Ok "已写 $reportPath"

Write-Host ''
Write-Host "快照完成：$OutDir" -ForegroundColor Cyan
Write-Host "回退时执行：powershell -NoProfile -File ops\deploy\cutover\rollback-to-01.ps1 -SnapshotDir `"$OutDir`"" -ForegroundColor DarkGray
