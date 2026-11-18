<#
  rollback-to-01.ps1 —— 生产切换失败时把线上拉回 D:\01_olbr_portal
  ============================================================================
  前提：切换前跑过 snapshot-prod-state.ps1，快照目录里有 state.json。

  它做什么（顺序固定，任何一步失败都会明确报出来）：
    1. 停掉 02 栈（只按 02 自己的 pid 文件停，不按端口盲杀）
    2. 等 80/443/7038/7090/9267 释放
    3. 把 02 的 .env 端口键恢复成快照里的旧值，并重生成 02 的 nginx.conf
       —— 让 02 退回「隔离副本」形态，避免下次误用生产端口启动
    4. 恢复 01 的守护计划任务（切换时被禁用的那些）并立刻触发一次
       —— 01 的自愈链自己会拉 PG / 后端 / worker / 前端 / 门户 / nginx
    5. 探活：7038 health、7090、9267、80、443 与 01 的 nginx -t
    6. 出 PASS/FAIL 汇总，全 PASS 才 exit 0

  它不做什么：不写生产库、不动 PostgreSQL（沿用系统安装的 5432，自愈链自己保证）、
  不删任何数据、不改 01 的 nginx.conf 与 .env（切换本就不该改它们）。

  用法：
    powershell -NoProfile -File ops\deploy\cutover\rollback-to-01.ps1 -DryRun
    powershell -NoProfile -File ops\deploy\cutover\rollback-to-01.ps1
    powershell -NoProfile -File ops\deploy\cutover\rollback-to-01.ps1 -SnapshotDir TMP\cutover\20261120-000500
    powershell -NoProfile -File ops\deploy\cutover\rollback-to-01.ps1 -Force   # 无视「看起来已是 01」的短路

  注意：停计划任务 / 改任务状态在部分机器上需要**管理员** PowerShell。
  非管理员时脚本不会硬失败，但会把「任务没恢复」记成 FAIL —— 看到就换管理员窗口重跑。
#>
[CmdletBinding()]
param(
    [string]$SnapshotDir = '',
    [switch]$DryRun,
    [switch]$Force,
    [int]$WaitSeconds = 180
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

function Write-Step { param([string]$m) Write-Host ""; Write-Host "[*] $m" -ForegroundColor Cyan }
function Write-Ok { param([string]$m) Write-Host "[OK] $m" -ForegroundColor Green }
function Write-Warn2 { param([string]$m) Write-Host "[WARN] $m" -ForegroundColor Yellow }
function Write-Bad { param([string]$m) Write-Host "[FAIL] $m" -ForegroundColor Red }

$Root02 = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..\..')).Path
$Root01 = 'D:\01_olbr_portal'
if ($Root02 -notlike '*02_olbr_portal') { throw "当前脚本不在 02 副本内（$Root02），拒绝执行。" }
if (-not (Test-Path -LiteralPath $Root01)) { throw "找不到生产副本 $Root01。" }

$script:Results = @()
function Add-Result {
    param([string]$Item, [bool]$Ok, [string]$Detail)
    $script:Results += [pscustomobject]@{ item = $Item; ok = $Ok; detail = $Detail }
    if ($Ok) { Write-Ok ("{0} :: {1}" -f $Item, $Detail) } else { Write-Bad ("{0} :: {1}" -f $Item, $Detail) }
}

# ---------- 选快照 ----------
if (-not $SnapshotDir) {
    $base = Join-Path $Root02 'TMP\cutover'
    $latest = Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'state.json') } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $latest) { throw "TMP\cutover 下没有可用快照。请先跑 snapshot-prod-state.ps1，或用 -SnapshotDir 指定。" }
    $SnapshotDir = $latest.FullName
}
if (-not (Test-Path -LiteralPath (Join-Path $SnapshotDir 'state.json'))) {
    throw "快照目录缺 state.json：$SnapshotDir"
}
$state = Get-Content -LiteralPath (Join-Path $SnapshotDir 'state.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Write-Host ("快照：{0}（采集于 {1}）" -f $SnapshotDir, $state.capturedAt) -ForegroundColor DarkGray

# ---------- 快照合理性校验 ----------
$envMap = $state.env02
$httpPort = 0
if ($envMap.PSObject.Properties.Name -contains 'NGINX_HTTP_PORT') { $httpPort = [int]$envMap.NGINX_HTTP_PORT }
if (($httpPort -eq 80 -or $httpPort -eq 443) -and (-not $Force)) {
    throw (("快照里的 NGINX_HTTP_PORT={0}，看起来是「切换后」的快照而不是切换前。" -f $httpPort) +
           " 用它回退会把 02 又设成生产端口。请指定正确的快照目录，或确认后加 -Force。")
}

# ---------- 工具函数 ----------
function Get-PortPid {
    param([int]$Port)
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return [int]$c.OwningProcess }
    return 0
}

function Wait-PortsFree {
    param([int[]]$Ports, [int]$Seconds)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $busy = @()
        foreach ($p in $Ports) { if ((Get-PortPid $p) -ne 0) { $busy += $p } }
        if ($busy.Count -eq 0) { return @() }
        Start-Sleep -Seconds 2
    }
    $busy = @()
    foreach ($p in $Ports) { if ((Get-PortPid $p) -ne 0) { $busy += $p } }
    return $busy
}

function Wait-PortsListen {
    param([int[]]$Ports, [int]$Seconds)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $missing = @()
        foreach ($p in $Ports) { if ((Get-PortPid $p) -eq 0) { $missing += $p } }
        if ($missing.Count -eq 0) { return @() }
        Start-Sleep -Seconds 3
    }
    $missing = @()
    foreach ($p in $Ports) { if ((Get-PortPid $p) -eq 0) { $missing += $p } }
    return $missing
}

function Test-HttpOk {
    param([string]$Url)
    try {
        $r = Invoke-WebRequest -Uri $Url -TimeoutSec 15 -UseBasicParsing
        return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 400)
    } catch { return $false }
}

function Set-EnvValues {
    param([string]$Path, [hashtable]$Values)
    $lines = [System.IO.File]::ReadAllLines($Path, [System.Text.Encoding]::UTF8)
    $seen = @{}
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($line in $lines) {
        if ($line -match '^\s*#' -or $line -notmatch '=') { $out.Add($line); continue }
        $k = ($line -split '=', 2)[0].Trim()
        if ($Values.ContainsKey($k)) {
            $out.Add(('{0}={1}' -f $k, $Values[$k]))
            $seen[$k] = $true
        } else {
            $out.Add($line)
        }
    }
    foreach ($k in $Values.Keys) {
        if (-not $seen.ContainsKey($k)) { $out.Add(('{0}={1}' -f $k, $Values[$k])) }
    }
    [System.IO.File]::WriteAllLines($Path, $out, (New-Object System.Text.UTF8Encoding($false)))
}

$ProdPorts = @(80, 443, 7038, 7090, 9267)

# ---------- 短路：看起来已经是 01 ----------
if (-not $Force) {
    $p80 = Get-PortPid 80
    $exe80 = ''
    if ($p80 -ne 0) {
        $pr = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $p80) -ErrorAction SilentlyContinue
        if ($pr) { $exe80 = [string]$pr.ExecutablePath }
    }
    $healthOk = Test-HttpOk 'http://127.0.0.1:7038/api/health'
    if ($exe80 -like '*01_olbr_portal*' -and $healthOk) {
        Write-Host '80 已由 01 的 nginx 持有、7038 health 正常——看起来已经在 01 上了。' -ForegroundColor Yellow
        Write-Host '如需强制重跑（例如任务状态没恢复干净），加 -Force。' -ForegroundColor Yellow
        exit 0
    }
}

Write-Host ''
Write-Host ('模式：{0}' -f $(if ($DryRun) { '干跑（不改任何东西）' } else { '实跑（会停 02、恢复 01）' })) -ForegroundColor Yellow
Write-Host ("02：{0}" -f $Root02) -ForegroundColor DarkGray
Write-Host ("01：{0}" -f $Root01) -ForegroundColor DarkGray

# ---------- 1. 停 02 ----------
Write-Step '1/6 停止 02 栈'
if ($DryRun) {
    Write-Host '    [干跑] 将执行：powershell -NoProfile -File ops\deploy\stop_all.ps1' -ForegroundColor DarkGray
} else {
    $stopScript = Join-Path $Root02 'ops\deploy\stop_all.ps1'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $stopScript
    Add-Result '停止 02 栈' ($LASTEXITCODE -eq 0) ("stop_all.ps1 退出码=$LASTEXITCODE")
}

# ---------- 2. 等端口释放 ----------
Write-Step '2/6 等待生产端口释放'
if ($DryRun) {
    Write-Host '    [干跑] 跳过等待' -ForegroundColor DarkGray
} else {
    $busy = Wait-PortsFree -Ports $ProdPorts -Seconds 60
    Add-Result '生产端口释放' ($busy.Count -eq 0) ($(if ($busy.Count -eq 0) { '80/443/7038/7090/9267 均空闲' } else { '仍被占用：' + ($busy -join ',') }))
}

# ---------- 3. 02 退回隔离形态 ----------
Write-Step '3/6 恢复 02 的 .env 端口并重生成 02 的 nginx.conf'
$envPath = Join-Path $Root02 '.env'
$restore = @{}
foreach ($k in $envMap.PSObject.Properties.Name) { $restore[$k] = [string]$envMap.$k }
if ($restore.Count -eq 0) {
    Write-Warn2 '快照里没有 .env 记录，跳过（02 可能仍是生产端口，请人工确认）'
    Add-Result '恢复 02 .env' $false '快照无 env02'
} elseif ($DryRun) {
    foreach ($k in $restore.Keys) { Write-Host ("    [干跑] {0}={1}" -f $k, $restore[$k]) -ForegroundColor DarkGray }
} else {
    if (-not (Test-Path -LiteralPath $envPath)) { throw "02 没有 .env：$envPath" }
    Copy-Item -LiteralPath $envPath -Destination ($envPath + '.bak_rollback') -Force
    Set-EnvValues -Path $envPath -Values $restore
    Add-Result '恢复 02 .env' $true ('已写回 ' + ($restore.Keys -join ', ') + '（备份 .env.bak_rollback）')
    $confScript = Join-Path $Root02 'ops\deploy\write_nginx_conf.ps1'
    if (Test-Path -LiteralPath $confScript) {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $confScript *> $null
        Add-Result '重生成 02 nginx.conf' ($LASTEXITCODE -eq 0) ("write_nginx_conf.ps1 退出码=$LASTEXITCODE")
    } else {
        Add-Result '重生成 02 nginx.conf' $false '找不到 write_nginx_conf.ps1'
    }
}

# ---------- 4. 恢复 01 的守护任务 ----------
Write-Step '4/6 恢复 01 的守护计划任务'
if ($DryRun) {
    foreach ($t in $state.tasks) { Write-Host ("    [干跑] Enable/Start {0}（快照状态 {1}）" -f $t.name, $t.state) -ForegroundColor DarkGray }
} else {
    $taskFail = @()
    foreach ($t in $state.tasks) {
        if ($t.state -eq 'Disabled') { Write-Host ("    {0} 在切换前就是停用状态，保持不动" -f $t.name) -ForegroundColor DarkGray; continue }
        try {
            $cur = Get-ScheduledTask -TaskName $t.name -ErrorAction Stop
            if ($cur.State -eq 'Disabled') {
                Enable-ScheduledTask -TaskName $t.name -ErrorAction Stop | Out-Null
            }
        } catch {
            $taskFail += ("{0}({1})" -f $t.name, $_.Exception.Message)
            continue
        }
        if ($t.name -match 'EnsureRunning') {
            try { Start-ScheduledTask -TaskName $t.name -ErrorAction Stop } catch { $taskFail += ("{0}(启动失败:{1})" -f $t.name, $_.Exception.Message) }
        }
    }
    if ($taskFail.Count -eq 0) { Add-Result '恢复守护任务' $true ('已处理 ' + $state.tasks.Count + ' 个任务') }
    else { Add-Result '恢复守护任务' $false ('失败：' + ($taskFail -join '; ') + '（非管理员时常见，请换管理员窗口重跑）') }
}

# ---------- 5. 探活 ----------
Write-Step '5/6 等待 01 起来并探活'
if ($DryRun) {
    Write-Host '    [干跑] 跳过探活' -ForegroundColor DarkGray
} else {
    $missing = Wait-PortsListen -Ports @(7038, 7090, 9267, 80, 443) -Seconds $WaitSeconds
    Add-Result '01 端口监听' ($missing.Count -eq 0) ($(if ($missing.Count -eq 0) { '7038/7090/9267/80/443 均在听' } else { '未起来：' + ($missing -join ',') + "（等 ${WaitSeconds}s）" }))

    $nginxConf = Join-Path $Root01 'nginx-1.28.0\conf\nginx.conf'
    if (Test-Path -LiteralPath $nginxConf) {
        Push-Location (Join-Path $Root01 'nginx-1.28.0')
        try {
            $t = & .\nginx.exe -t 2>&1
            Add-Result '01 nginx -t' ($LASTEXITCODE -eq 0) (($t | Select-Object -Last 1) -replace "`r?`n", ' ')
        } finally { Pop-Location }
    } else {
        Add-Result '01 nginx -t' $false '找不到 01 的 nginx.conf'
    }

    Add-Result '01 后端 /api/health' (Test-HttpOk 'http://127.0.0.1:7038/api/health') 'http://127.0.0.1:7038/api/health'
    Add-Result '01 前端 7090' (Test-HttpOk 'http://127.0.0.1:7090/myapp/') 'http://127.0.0.1:7090/myapp/'
    Add-Result '01 门户 9267' (Test-HttpOk 'http://127.0.0.1:9267/uniplam/') 'http://127.0.0.1:9267/uniplam/'
    Add-Result '对外 80 /myapp/' (Test-HttpOk 'http://127.0.0.1/myapp/') 'http://127.0.0.1/myapp/'
}

# ---------- 6. 汇总 ----------
Write-Step '6/6 汇总'
$failed = @($script:Results | Where-Object { -not $_.ok })
if ($DryRun) {
    Write-Host '干跑结束：未做任何改动。' -ForegroundColor Yellow
    exit 0
}
Write-Host ''
Write-Host ('结果：{0} 项，PASS {1}，FAIL {2}' -f $script:Results.Count, ($script:Results.Count - $failed.Count), $failed.Count) -ForegroundColor Cyan
if ($failed.Count -gt 0) {
    Write-Host '未通过项：' -ForegroundColor Red
    foreach ($f in $failed) { Write-Host ('  - {0} :: {1}' -f $f.item, $f.detail) -ForegroundColor Red }
    Write-Host ''
    Write-Host '处理建议：' -ForegroundColor Yellow
    Write-Host '  1) 先在管理员 PowerShell 里重跑本脚本（任务恢复常需要管理员）'
    Write-Host '  2) 仍失败则手工执行 D:\01_olbr_portal\Task\my_app\start.bat，再看 log\ 下的日志'
    Write-Host '  3) nginx 未起：cd D:\01_olbr_portal\nginx-1.28.0; .\nginx.exe -t; .\nginx.exe'
    exit 1
}
Write-Host '已回到 01，全部探活通过。' -ForegroundColor Green
exit 0
